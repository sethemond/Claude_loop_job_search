# Job Scout — Dependency Chain & File Audit

> Generated: 2026-05-23; Updated: 2026-05-25  
> Scope: All files in the project root and sub-folders, excluding `scripts/DEAD scripts/` (user-excluded) and `.git/`.  
> Purpose: Map every live dependency chain; identify files that can be removed.

---

## Table of Contents

1. [Dependency Chain](#1-dependency-chain)  
   1a. [Runtime chain (serve.py and everything it touches)](#1a-runtime-chain)  
   1b. [Test chain](#1b-test-chain)  
   1c. [Standalone utilities](#1c-standalone-utilities)  
   1d. [Data files](#1d-data-files)  
   1e. [Static assets read by Claude](#1e-static-assets-read-by-claude)  
2. [Complete File Inventory](#2-complete-file-inventory)  
3. [Orphaned / Removable Files](#3-orphaned--removable-files)  
4. [Missing Files Referenced by Code](#4-missing-files-referenced-by-code)  
5. [Double-Check Notes](#5-double-check-notes)

---

## 1. Dependency Chain

### 1a. Runtime Chain

```
scripts/serve.py                        ← python scripts/serve.py  (main entry point)
│
├── IMPORT: search_scripts/schedule_manager.py
│     └── IMPORT: search_scripts/loop_manager.py
│           ├── IMPORT: search_scripts/claude_call.py
│           │     └── SUBPROCESS: claude binary (external)
│           ├── IMPORT: search_scripts/check_claude_usage.py
│           │     └── SUBPROCESS/IMPORT: claude-monitor (external uv tool)
│           ├── IMPORT: search_scripts/context_usage.py
│           │     └── READS: ~/.claude/projects/**/<session_id>.jsonl  (external)
│           └── IMPORT: search_scripts/count_jobs.py
│                 └── READS: jobs.json
│
├── DYNAMIC IMPORT (at /usage request): scripts/pull_usage_data.py
│     └── SUBPROCESS/IMPORT: claude-monitor (external uv tool)
│
├── SUBPROCESS (TEST_MODE=True, POST /run-batch, POST /continue-run):
│     scripts/test_claude_call.py       ← FILE DOES NOT EXIST (see §4)
│
├── SUBPROCESS (TEST_MODE=False, fallback):
│     scripts/run_batch.py
│           ├── READS: search_queue.json
│           ├── READS: workflow.md
│           ├── READS/WRITES: run_state.json
│           └── SUBPROCESS: claude binary (external)
│
├── SERVES: dashboard.html              ← browser single-page app
│     ├── CALLS (HTTP fetch): all endpoints on serve.py
│     ├── LAZY LOADS (fetch): tabs/tab-run.html  ← injected into #tab-run on first open
│     └── LOADS (script defer): tabs/tab-run.js  ← all Run-tab JS
│
├── READS (GET /profile):              profile.md
├── READS/WRITES (GET|POST /usage-limits): usage_limits.json  ← FILE DOES NOT EXIST (see §4)
├── READS/WRITES: jobs.json, tags.json, status.json, search_queue.json
├── READS/WRITES: run_state.json, loop_state.json, schedules.json, runs.json
├── READS/WRITES: test_session.json, smart_apply_config.json
│
├── REFERENCES (POST /update-profile):  scripts/update_profile_prompt.md
│     └── SUBPROCESS: claude binary  →  WRITES: profile.md
│     └── TEMP WRITES: temp_document.txt  (transient, deleted after use)
│
├── REFERENCES (POST /run-dedup):       scripts/dedup_tags_prompt.md
│     └── SUBPROCESS: claude binary  →  READS/WRITES: tags.json
│
└── BACKGROUND THREAD: _auto_restart_loop()
      └── SUBPROCESS (when rate_limited): scripts/test_claude_call.py
            └── FILE DOES NOT EXIST (see §4)
```

**Claude runs (via claude_call.py subprocess) can call:**
```
scripts/tag_create.py                   ← called by Claude inside a session
      └── READS/WRITES: tags.json
```

---

### 1b. Test Chain

```
search_scripts/tests/test_loop_manager.py       (python test_loop_manager.py)
      └── IMPORT: search_scripts/loop_manager   → full loop_manager chain (mocked)
      └── IMPORT: search_scripts/claude_call    (STATUS_OK, STATUS_LIMITS, STATUS_ERROR)
      STATUS: ✅ Active — 87 tests, all pass

search_scripts/tests/test_loop_manager_integration.py
      └── IMPORT: search_scripts/loop_manager   (real Claude calls, skipped if no binary)
      └── IMPORT: search_scripts/claude_call    (find_claude)
      └── WRITES: logs/integration/<timestamp>/  (generated output)
      └── WRITES: logs/integration/runs_integration.json
      STATUS: ✅ Active — real integration tests, skipped without claude binary

search_scripts/tests/test_schedule_manager.py
      └── IMPORT: search_scripts/schedule_manager → full schedule_manager chain (mocked)
      STATUS: ✅ Active — 83 tests, all pass
```

---

### 1c. Standalone Utilities

These scripts are not imported by anything but are run manually or via Claude Code sessions.
They are in the dependency chain as operational tools.

```
scripts/generate_queue.py
      └── WRITES: search_queue.json
      STATUS: ✅ Utility — seeds the search queue; listed in .claude/settings.local.json

scripts/restart_server.py
      └── SUBPROCESS: scripts/serve.py
      STATUS: ✅ Utility — listed in .claude/settings.local.json
```

---

### 1d. Data Files

All JSON data files written/read by the runtime chain. All exist unless noted.

| File | Written by | Read by | Status |
|---|---|---|---|
| `jobs.json` | Claude (via claude_call) | serve.py, count_jobs.py, dashboard | ✅ |
| `tags.json` | Claude, tag_create.py, serve.py | serve.py, Claude, dashboard | ✅ |
| `status.json` | serve.py | serve.py, dashboard | ✅ |
| `search_queue.json` | serve.py, generate_queue.py | serve.py, run_batch.py, dashboard | ✅ |
| `usage_limits.json` | serve.py | serve.py, dashboard | ❌ Missing (see §4) |
| `run_state.json` | serve.py, run_batch.py | serve.py, dashboard | ✅ |
| `loop_state.json` | schedule_manager.py | serve.py, dashboard | ✅ |
| `schedules.json` | schedule_manager.py | serve.py, dashboard | ✅ |
| `runs.json` | loop_manager.py | schedule_manager.py, serve.py, dashboard | ✅ |
| `test_session.json` | test_claude_call.py (deleted) | test_claude_call.py (deleted) | ☠️ Legacy — deleted 2026-05-25 |
| `smart_apply_config.json` | serve.py | serve.py, dashboard | ✅ |

---

### 1e. Static Assets Read by Claude

Files read by Claude during sessions or by serve.py to build prompts:

| File | Used by | Purpose |
|---|---|---|
| `workflow.md` | run_batch.py, build_loop_config() in serve.py | Claude's job-search instructions |
| `profile.md` | serve.py (GET /profile, POST /profile) | Seth's background, served to dashboard |
| `CLAUDE.md` | Claude Code agent | Agent security/permissions |
| `scripts/update_profile_prompt.md` | serve.py POST /update-profile | Prompt for Claude to update profile |
| `scripts/dedup_tags_prompt.md` | serve.py POST /run-dedup | Prompt for Claude to consolidate tags |
| `Job App Info/Seth Emond Resume.docx` | serve.py smart-apply (path from config) | Resume copied to application folders |
| `Job App Info/Cover Letter - Role Transition - Design.docx` | serve.py smart-apply (path from config) | Cover letter copied to application folders |
| `tabs/tab-run.html` | serve.py (route /tabs/tab-run.html) | Run tab inner HTML; lazy-loaded by showTab('run') and injected into #tab-run |
| `tabs/tab-run.js` | serve.py (route /tabs/tab-run.js) | All Run-tab JavaScript; loaded via `<script defer>` in dashboard.html |

---

## 2. Complete File Inventory

Legend: ✅ In chain | 🔧 Utility | 📄 Docs/planning | 🔴 Broken | ☠️ Orphaned | 🗂️ Generated

### Root directory

| File | Status | Notes |
|---|---|---|
| `CLAUDE.md` | ✅ | Read by Claude Code agent; security rules |
| `architecture.md` | 📄 | Project documentation |
| `dashboard.html` | ✅ | Served by serve.py as the UI |
| `job_scout_plan_v2.md` | ☠️ | Old planning doc; only referenced in DEAD scripts/run.ps1 |
| `jobs.json` | ✅ | Core data file |
| `loop_state.json` | ✅ | Runtime state written by schedule_manager.py |
| `profile.md` | ✅ | Served and written by serve.py |
| `run_state.json` | ✅ | Runtime state written by run_batch.py / serve.py |
| `runs.json` | ✅ | Written by loop_manager.py |
| `schedules.json` | ✅ | Written by schedule_manager.py |
| `search_queue.json` | ✅ | Read/written by serve.py |
| `smart_apply_config.json` | ✅ | Read/written by serve.py |
| `status.json` | ✅ | Read/written by serve.py |
| `tags.json` | ✅ | Read/written by Claude, tag_create.py, serve.py |
| `dependency_chain.md` | 📄 | This file; project file map |
| `to-do.md` | 📄 | Project task list; not used by code |
| `usage_limits.json` | ❌ | Referenced by serve.py but MISSING (see §4) |
| `workflow.md` | ✅ | Read by run_batch.py and build_loop_config() |
| `workflow_v2.md` | ☠️ | Draft/alternative workflow; no code references it |

### `tabs/`

| File | Status | Notes |
|---|---|---|
| `tab-run.html` | ✅ | Run tab inner HTML; served at `/tabs/tab-run.html`; injected lazily into `#tab-run` by `showTab('run')` |
| `tab-run.js` | ✅ | All Run-tab JavaScript; served at `/tabs/tab-run.js`; loaded with `<script defer>` in dashboard.html |

### `Job App Info/`

| File | Status | Notes |
|---|---|---|
| `Seth Emond Resume.docx` | ✅ | Path stored in smart_apply_config.json; copied by serve.py smart-apply |
| `Cover Letter - Role Transition - Design.docx` | ✅ | Same as above |

### `docs/`

| File | Status | Notes |
|---|---|---|
| `claude-cli-reference.md` | ☠️ | External reference copy; no code references it |
| `cli-reference.md` | ☠️ | External reference copy; no code references it; near-duplicate of above |

### `scripts/`

| File | Status | Notes |
|---|---|---|
| `serve.py` | ✅ | Main server entry point |
| `run_batch.py` | ✅ | Spawned by serve.py when TEST_MODE=False |
| `pull_usage_data.py` | ✅ | Dynamically imported by serve.py for GET /usage |
| `tag_create.py` | ✅ | Subprocess-called by Claude during sessions |
| `generate_queue.py` | 🔧 | Standalone utility; writes search_queue.json |
| `restart_server.py` | 🔧 | Standalone utility; restarts serve.py |
| `update_profile_prompt.md` | ✅ | Referenced by serve.py POST /update-profile |
| `dedup_tags_prompt.md` | ✅ | Referenced by serve.py POST /run-dedup |

### `scripts/tests/`

_Directory removed 2026-05-25. All three files (`run_tests.py`, `test_scheduling.py`, `test_thresholds.py`) imported deleted modules (`batch_loop.py`, `test_claude_call.py`) and were non-functional._

### `scripts/__pycache__/`

| File | Status | Notes |
|---|---|---|
| `serve.cpython-314.pyc` | 🗂️ | Generated cache for serve.py |
| `run_batch.cpython-314.pyc` | 🗂️ | Generated cache for run_batch.py |
| `pull_usage_data.cpython-311.pyc` | 🗂️ | Generated cache for pull_usage_data.py |
| `pull_usage_data.cpython-314.pyc` | 🗂️ | Generated cache for pull_usage_data.py |

### `search_scripts/`

| File | Status | Notes |
|---|---|---|
| `schedule_manager.py` | ✅ | Imported by serve.py; owns all scheduling |
| `loop_manager.py` | ✅ | Imported by schedule_manager.py |
| `claude_call.py` | ✅ | Imported by loop_manager.py |
| `check_claude_usage.py` | ✅ | Imported by loop_manager.py |
| `context_usage.py` | ✅ | Imported by loop_manager.py |
| `count_jobs.py` | ✅ | Imported by loop_manager.py |

### `search_scripts/__pycache__/`

All `.pyc` files here are 🗂️ Generated caches for active source files (all ✅).

### `search_scripts/tests/`

| File | Status | Notes |
|---|---|---|
| `test_loop_manager.py` | ✅ | 87 tests, all pass |
| `test_loop_manager_integration.py` | ✅ | Real Claude integration tests |
| `test_schedule_manager.py` | ✅ | 83 tests, all pass |

### `logs/`

All files in `logs/` are 🗂️ Generated output — written by loop_manager.py, claude_call.py, run_batch.py, and integration tests. Not removed under normal operations; accumulate over time.

Notable:
- `logs/integration/` — generated by test_loop_manager_integration.py
- `logs/integration/runs_integration.json` — generated by integration tests
- `logs/loop_log_*.txt` — orchestration logs from loop_manager.py
- `logs/test_*.log`, `logs/my_run.log`, `logs/claude_call_test.log`, `logs/test_usage.txt` — one-off test outputs

### `.claude/`

| File | Status | Notes |
|---|---|---|
| `settings.local.json` | ✅ | Claude Code permission allowlist |

---

## 3. Orphaned / Removable Files

> **Cleanup 2026-05-25:** Categories A, B, and C (and `test_session.json`) were all deleted.
> Only Category D remains pending manual review.

### Category D — Orphaned Documentation / Planning Files (pending manual review)

These markdown files are not referenced by any code, script, or active documentation.

| File | Notes |
|---|---|
| `job_scout_plan_v2.md` | Original project plan. Only referenced in now-deleted `DEAD scripts/run.ps1`. Superseded by architecture.md and to-do.md. |
| `workflow_v2.md` | Draft/alternative Claude workflow. Not referenced by run_batch.py, serve.py, or build_loop_config(). Only mentioned in to-do.md as a future file to create. |
| `docs/claude-cli-reference.md` | Copy of external Claude CLI documentation. Not referenced by any code. Reference only. |
| `docs/cli-reference.md` | Near-duplicate of above. Not referenced by any code. Reference only. |

### Previously removed

| File | Category | Removed |
|---|---|---|
| `_diag.py` | A — orphaned source | 2026-05-25 |
| `scripts/compare_methods.py` | A — orphaned source (also missing `test_jobs_sample.json` dep) | 2026-05-25 |
| `test_session.json` | A — legacy; only used by deleted `test_claude_call.py` | 2026-05-25 |
| `scripts/tests/` (whole directory) | B — all three files imported deleted modules | 2026-05-25 |
| `scripts/__pycache__/batch_loop.cpython-314.pyc` | C — stale cache, source deleted | 2026-05-25 |
| `scripts/__pycache__/test_claude_call.cpython-314.pyc` | C — stale cache, source deleted | 2026-05-25 |
| `scripts/__pycache__/check_claude_usage.cpython-314.pyc` | C — stale cache, source moved to search_scripts/ | 2026-05-25 |
| `scripts/__pycache__/compare_methods.cpython-314.pyc` | C — stale cache, source deleted | 2026-05-25 |

---

## 4. Missing Files Referenced by Code

These files are referenced by active code but **do not exist** in the project. They are not candidates for removal — they need to be created.

| Missing file | Referenced by | Effect of absence |
|---|---|---|
| `scripts/test_claude_call.py` | `serve.py` lines 185, 792, 1285 — spawned when `TEST_MODE=True` (which is the current setting) | POST /run-batch and POST /continue-run silently fail or error at subprocess spawn time |
| `usage_limits.json` | `serve.py` GET /usage-limits and POST /usage-settings | GET /usage-limits returns an error; POST /usage-settings cannot read existing settings |

---

## 5. Double-Check Notes

**Cross-check: every Python import statement traced**

| Import | Found in source | Resolves to | Status |
|---|---|---|---|
| `search_scripts.schedule_manager` | serve.py | search_scripts/schedule_manager.py | ✅ |
| `search_scripts.loop_manager` | schedule_manager.py | search_scripts/loop_manager.py | ✅ |
| `search_scripts.claude_call` | loop_manager.py, test_loop_manager.py, test_loop_manager_integration.py | search_scripts/claude_call.py | ✅ |
| `search_scripts.check_claude_usage` | loop_manager.py | search_scripts/check_claude_usage.py | ✅ |
| `search_scripts.context_usage` | loop_manager.py | search_scripts/context_usage.py | ✅ |
| `search_scripts.count_jobs` | loop_manager.py | search_scripts/count_jobs.py | ✅ |
| `search_scripts.loop_manager` | test_loop_manager.py, test_loop_manager_integration.py | search_scripts/loop_manager.py | ✅ |
| `search_scripts.schedule_manager` (module) | test_schedule_manager.py | search_scripts/schedule_manager.py | ✅ |
| `pull_usage_data` | serve.py (dynamic, inside function) | scripts/pull_usage_data.py | ✅ |
| `claude_monitor.*` | check_claude_usage.py, pull_usage_data.py | External uv tool; not a project file | external |

**Cross-check: every subprocess call traced**

| Caller | Command | Target file | Status |
|---|---|---|---|
| serve.py _post_run_batch() | scripts/test_claude_call.py | scripts/test_claude_call.py | ❌ Missing |
| serve.py _auto_restart_loop() | scripts/test_claude_call.py | scripts/test_claude_call.py | ❌ Missing |
| serve.py _post_run_batch() (TEST_MODE=False) | scripts/run_batch.py | scripts/run_batch.py | ✅ |
| serve.py _post_update_profile() | claude binary | external | ✅ |
| serve.py _post_run_dedup() | claude binary | external | ✅ |
| serve.py _post_open_editor() | code (VS Code) | external | ✅ |
| run_batch.py | claude binary | external | ✅ |
| claude_call.py | claude binary | external | ✅ |
| check_claude_usage.py | uv tool run | external | ✅ |
| pull_usage_data.py | uv tool run | external | ✅ |
| restart_server.py | scripts/serve.py | scripts/serve.py | ✅ |

**Cross-check: every data file reference confirmed**

All JSON data files in the root that are read by active code:
`jobs.json` ✅ · `tags.json` ✅ · `status.json` ✅ · `search_queue.json` ✅ ·
`run_state.json` ✅ · `loop_state.json` ✅ · `schedules.json` ✅ · `runs.json` ✅ ·
`smart_apply_config.json` ✅ · `usage_limits.json` ❌ missing · `test_session.json` ☠️ deleted

**Confirmed orphaned (no false positives):**

- `workflow_v2.md` — searched all `.py` and `.html` for references: none found.
- `job_scout_plan_v2.md` — only reference was inside now-deleted `DEAD scripts/run.ps1`.
- `docs/claude-cli-reference.md` and `docs/cli-reference.md` — searched all files for any reference: none found.
