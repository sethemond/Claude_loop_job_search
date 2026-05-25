# Job Scout — System Architecture & Flow

> Human-readable reference for how every component works, what feeds into it,
> and what it produces. See inline code docstrings for implementation detail.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Job Listing & Ranking](#2-job-listing--ranking)
3. [Claude Calls and Automatic Looping](#3-claude-calls-and-automatic-looping)
4. [Web Dashboard / UI Elements](#4-web-dashboard--ui-elements)
5. [Session Management](#5-session-management)
6. [Queue Management](#6-queue-management)
7. [Data Files Reference](#7-data-files-reference)

---

## 1. System Overview

Job Scout automates a job search for Seth by repeatedly calling the Claude CLI,
which in turn searches Indeed, evaluates listings against a tag registry, and
writes structured results to JSON files. A browser dashboard provides controls
and displays results in real time.

```
 Browser (dashboard.html)
       │
       │  HTTP  GET/POST
       ▼
 serve.py  ──────────────────────────────────────────────────────────┐
   │                                                                  │
   │  Background threads (in-process)                                 │
   │    ScheduleManager tick thread — adaptive sleep; wakes at the    │
   │      exact second of each scheduled run; fires run_loop() via    │
   │      a ThreadPoolExecutor (up to 8 concurrent schedules)         │
   │    auto-restart thread — 30s poll; re-spawns test_claude_call.py │
   │      when rate_limited and resume_at has passed                  │
   │                                                                  │
   │  On-demand (dashboard button press)                              │
   │    POST /schedule-loop → build_loop_config() → schedule_loop()   │
   └──────────────────────────────────────────────────────────────────┘
                │
                ▼
         schedule_manager.py  (search_scripts/)
           schedule_loop() → persists entry to schedules.json
           tick thread fires _execute() in thread pool when next_run ≤ now
           _execute() calls run_loop(), then writes status back to schedules.json
                │
                ▼
         loop_manager.py   (search_scripts/)
           Per dynamic prompt: check thresholds → build prompt → call Claude
           Logs activity to loop_log_<ts>.txt
           Appends record to runs.json after each call
                │
                ▼
         claude_call.py   (search_scripts/)
           Launches: claude -p <prompt> --output-format stream-json [--resume <id>]
           Parses stream: captures session_id, Claude output, usage stats, limit signals
           Writes per-call log: loop_log_<ts>_claude_<n>.txt
           Returns: (status, session_id, context_tokens)
                │
                ▼
         Claude CLI  →  Indeed MCP  →  jobs.json / tags.json
```

**Key design principles:**
- Claude does all semantic work (reading JDs, applying tags, writing job records).
  Python handles all orchestration, cost tracking, state management, and the UI.
- All state is files (JSON). No database, no in-memory state between runs.
- Every stop condition exits cleanly, writing an explanation to run_state.json
  so the dashboard and the auto-restart logic know what happened.

---

## 2. Job Listing & Ranking

This is what Claude actually does when it runs — the instructions live in `workflow.md`.

### 2a. Context Loading

Claude reads three files at the start of every run:

| File | Purpose |
|---|---|
| `profile.md` | Seth's skills, target industries, hard filters (no senior/lead roles) |
| `jobs.json` | Existing job database — used for fingerprint dedup and seeing existing tags |
| `tags.json` | Complete tag registry with weights, aliases, categories |

**Input:** workflow.md + the single batch entry (keyword, location)
**Output:** none yet — this step only loads context into Claude's session

---

### 2b. Job Search

Claude calls `mcp__claude_ai_Indeed__search_jobs` with:
- `search`: keyword from the queue entry (e.g. "mechanical design engineer")
- `location`: location from the queue entry (e.g. "Charleston, SC")
- `country_code`: "US"
- `job_type`: "fulltime"

Indeed returns a list of job postings. Each posting has: title, company,
location, salary, URL, posting date, indeed_id.

**Input:** keyword + location from the current queue entry
**Output:** raw list of job postings from Indeed

---

### 2c. Fingerprint Deduplication

***[this is stupid, indeed search returns a unique job ID - just use that we don't need to extra work]***

For each job returned by Indeed, Claude computes a fingerprint:

```
fingerprint = slug(company) + "|" + slug(title) + "|" + slug(location)
slug = lowercase, letters and digits only, all spaces/punctuation removed
```

Claude checks `jobs.json` for a matching fingerprint:
- **Match found:** update `last_seen` to today; skip to next job (no re-tagging)
- **No match:** create a new job entry with `first_seen = last_seen = today`,
  `score = null`, `tier = null`, `tags = []`

This prevents the same position from accumulating duplicate entries as it
re-appears across multiple weekly searches.

**Input:** job posting from Indeed + existing jobs.json fingerprints
**Output:** determination of new vs. seen; new entries queued for tagging

---

### 2d. Title Filtering

Before tagging, Claude checks the title for disqualifying words.
Jobs are **skipped entirely** (not added to jobs.json) if the title contains:

> senior, lead, manager, principal, director, vp, vice president,
> superintendent, sales, application specialist

**Input:** job title string
**Output:** include or discard decision for this job

---

### 2e. Tag Application

For each new job that passes the title filter, Claude fetches the full job
description via `mcp__claude_ai_Indeed__get_job_details(job_id)`, then applies
tags from `tags.json`.

**Tag categories:**

| Category | Color | Example tags |
|---|---|---|
| Skills Match | green | `solidworks`, `fdm-3dprint`, `python`, `fea` |
| Experience Level | amber | `exp-0-2-years`, `exp-2-3-years`, `exp-5-plus-years` |
| Industry | purple | `medical-devices`, `robotics-automation`, `marine` |
| Location | blue | `loc-charleston-sc`, `loc-coastal-fl` |
| Salary | green | `salary-65-75k`, `salary-above-90k` |
| Culture / Fit | cyan | `ownership-selfdirected`, `portfolio-valued` |
| Avoid / Penalty | red | `security-clearance`, `no-remote` |
| Skill & Knowledge Fit | pink | `skill-match-high`, `knowledge-match-low` |

**Matching rules:**
- Apply only on a clear, confident match — never on uncertainty
- Multiple tags from the same category are allowed
- Location tags match the job's actual location, not the search location
- Exactly one `skill-match-*` and one `knowledge-match-*` tag per job

**Skill-match logic:**

| Tag | Weight | Criteria |
|---|---|---|
| `skill-match-high` | +15 | Requires SolidWorks AND at least one of: FDM/3D printing, mechanism design, Python, prototyping |
| `skill-match-mid` | +6 | Requires SolidWorks OR one strong-skill area, with notable gaps elsewhere |
| `skill-match-low` | −8 | Core requirements are primarily areas Seth hasn't mastered (CATIA, heavy ANSYS FEA, electrical/controls) |

**Knowledge-match logic:**

| Tag | Weight | Criteria |
|---|---|---|
| `knowledge-match-high` | +10 | Medical devices, robotics, additive manufacturing, marine, consumer products |
| `knowledge-match-mid` | +4 | Adjacent industries: aerospace/defense, sports tech, prosthetics |
| `knowledge-match-low` | −6 | Outside Seth's background: HVAC, heavy construction, oil & gas, semiconductor fab |

**Input:** full job description text + tags.json registry
**Output:** list of tag IDs applied to this job

---

### 2f. New Tag Proposals

If Claude finds a meaningful recurring characteristic not covered by any existing tag,
it calls `scripts/tag_create.py` to propose a new tag:

```
tag_create.py --id "kebab-case-id" --label "Human Label" --weight N
              --category skills|experience|industry|location|salary|culture|avoid|fit
              --description "When does this tag apply?"
              --aliases "synonym1,synonym2"
              --reason "Which job triggered this"
```

New tags start with `status: "pending"` and appear in the dashboard's
**Pending Tags** panel for review (approve / reject / merge).
Pending tags are included in scoring immediately — approval is for cleanup only.

**Input:** unmatched job characteristic + existing tag registry
**Output:** new entry added to `tags.json` with `status: "pending"`

---

### 2g. Score Computation

Scores are computed by the dashboard JavaScript when rendering the job list:

```
score = base_score + sum(tag.weight for tag in job.tags)
base_score = 50  (defined in tags.json)
```

No clamping — negative scores are possible for heavily-penalized jobs.

**Tier assignment** (by percentile rank across the current job pool):
- **Tier 1** — top 25% of scores
- **Tier 2** — middle 50%
- **Tier 3** — bottom 25%

Tiers are recomputed every time the dashboard loads `/jobs`.

**Input:** jobs.json tag arrays + tags.json weights + base_score
**Output:** numeric score and tier label rendered on each job card

---

### 2h. Write jobs.json

[this should be a "POST" script that claude calls - claude hands off the jobs info as json to script. script handles adding to json, checking for json consistency (returns error code to claude if syntax error), This prevents A) unneccessary token usage B) better/cleaner handeling of permission - prevents claude messing anything up]

At the end of every Claude session, Claude writes the complete updated `jobs.json`
(all jobs, including unchanged ones, to preserve the full database state).

**Input:** in-memory job list (updated from search results)
**Output:** `jobs.json` overwritten with all jobs (new + updated `last_seen` on existing)

---

## 3. Claude Calls and Automatic Looping

---

### 3a. claude_call.py — Single Claude CLI Invocation

`search_scripts/claude_call.py` is a standalone wrapper around the Claude CLI.
It is called by `loop_manager.py` for every individual Claude call.

**Usage (programmatic):**
```python
from search_scripts.claude_call import run_claude, STATUS_OK, STATUS_LIMITS, STATUS_ERROR
status, session_id, context_tokens = run_claude(prompt, log_path, session_id=None)
```

**What it does:**
1. Locates the `claude` binary (PATH, then common install fallbacks).
2. Builds the subprocess command:
   ```
   claude -p <prompt> --output-format stream-json --verbose --include-partial-messages
          [--resume <session_id>]
   ```
3. Streams JSON events from the Claude CLI, writing a timestamped log to `log_path`:
   - `system/init` → captures `session_id` and model name
   - `stream_event` → streams thinking blocks, Claude text output, and tool calls
   - `tool_result` → logs a truncated preview of tool output
   - `result` → captures final usage stats; detects limit keywords in result text
   - `system/api_retry` → flags `rate_limit` / `max_output_tokens` as limit signals
4. Returns `(status, session_id, context_tokens)`.

**Return values:**

| `status` | Meaning |
|---|---|
| `STATUS_OK` (1) | Claude completed successfully |
| `STATUS_LIMITS` (2) | A usage, context, or rate limit was hit |
| `STATUS_ERROR` (0) | Unexpected failure (bad exit code, launch error) |

**Subprocess termination:**

A module-level `_active_proc` variable (protected by `_active_proc_lock`) holds the
currently running `subprocess.Popen` object. It is set immediately after `Popen` succeeds
and cleared in a `finally` block after `proc.wait()` returns.

`terminate_active()` kills the active subprocess if one is running:
```python
from search_scripts.claude_call import terminate_active
killed = terminate_active()   # True if a process was killed
```
Sends `SIGTERM`, waits up to 5 s, then sends `SIGKILL` if still alive.
Called by `schedule_manager.cancel_schedule()` when a running schedule is cancelled,
enabling immediate Stop Loop behaviour rather than waiting for the current call to finish.

**Input:** prompt string, log file path, optional session UUID
**Output:** `(status, session_id, context_tokens)` + timestamped log file written to disk

---

### 3b. loop_manager.py — Multi-Turn Loop Orchestrator

`search_scripts/loop_manager.py` drives a sequence of Claude calls across a list of
dynamic prompts, checking usage and context thresholds between each call.

**Usage (programmatic):**
```python
from search_scripts.loop_manager import run_loop

result = run_loop(
    primary_prompt         = "...",   # sent on first call / new session
    nextLoop_prompt_static  = "...",   # prepended to every loop
    nextLoop_prompt_dynamic = ["...", "..."],  # one Claude call per entry
    session_threshold      = 80.0,
    weekly_threshold       = 80.0,
    context_threshold      = 90.0,
    session_id             = None,    # or existing UUID to resume
    allow_reschedule       = False,
)
# result keys: success, limit_exceeded, remaining_loop_prompts, reschedule_time
```

**Per-loop execution sequence:**

| Step | Action |
|---|---|
| [1] | Query `check_claude_usage.get_usage_snapshot()` — stop if `session_pct >= session_threshold` |
| [2] | Same snapshot — stop if `weekly_pct >= weekly_threshold` |
| [3] | Query `context_usage.get_context_usage(session_id)` — if `pct >= context_threshold`, reset `session_id = None` |
| [4] | Read `jobs.json` via `count_jobs.get_job_count()` → `jobs_before` |
| [5] | Build prompt (see session modes below) |
| [6] | Call `claude_call.run_claude(prompt, claude_log_path, session_id)` |
| [7] | Read `jobs.json` again → `jobs_after`; compute `jobs_new` |
| [8] | Query post-call usage snapshot for cost figures |
| [9] | Append record to `runs.json` |
| [10] | On success: call `_mark_queue_entry_done(keyword, location, timestamp)` to write `status="done"` and `last_run` back to `search_queue.json` |
| [11] | Handle non-OK status: `LIMITS` → `limit_exceeded=3`; `ERROR` → `success=0`; both stop the loop |

**Queue entry completion (`_mark_queue_entry_done`):**

After a successful iteration, `loop_manager` extracts the keyword and location from the
dynamic prompt string via `_parse_kw_loc()` and writes `status="done"` + `last_run` back
to the matching `search_queue.json` entry. This ensures the queue reflects which searches
have been run so the batch selection logic skips them until `rerun_after_days` passes.

`_parse_kw_loc` splits on the `", location:"` delimiter first (handling locations that
contain a comma, e.g. `"Charleston, SC"`), then falls back to generic key-value parsing.

**Session modes and prompt construction:**

| Mode | Condition | Prompt sent to Claude |
|---|---|---|
| New session | `session_id=None` | `primary_prompt + static + dynamic[i]` |
| Resumed session | `session_id` provided, context OK | `static + dynamic[i]` |
| Context reset | `context_pct >= context_threshold` mid-loop | `session_id` cleared; next call uses new-session form |
| Continue from limit | `continuing_from_limit_reached=True`, first call only | `continue_prompt` (exact, replaces normal prompt) |

**Return value:**

| Key | Type | Meaning |
|---|---|---|
| `success` | int | 1 = completed or graceful stop; 0 = Claude error |
| `limit_exceeded` | int | 0=none, 1=session threshold, 2=weekly threshold, 3=unexpected API limit |
| `remaining_loop_prompts` | list | Unprocessed dynamic prompts; pass back on next scheduled run |
| `reschedule_time` | str\|None | ISO UTC session-reset time, if `allow_reschedule=True` and a limit was hit |

**Log file naming** (default, no `claude_log_file` provided):
```
logs/loop_log_<YYYYMMDD_HHMMSS>.txt           ← orchestration log (one per run)
logs/loop_log_<YYYYMMDD_HHMMSS>_claude_1.txt  ← Claude output for loop 1
logs/loop_log_<YYYYMMDD_HHMMSS>_claude_2.txt  ← Claude output for loop 2
...
```
Files share the same timestamp prefix and sort together in directory listings.

**Input:** prompts, thresholds, optional session_id
**Output:** result dict + loop log + per-call Claude logs + appended records in `runs.json`

---

### 3c. schedule_manager.py — Concurrent Multi-Schedule Orchestrator

`search_scripts/schedule_manager.py` owns all scheduled and immediate `run_loop()` calls.
It is started once at `serve.py` startup via `get_manager()` and runs for the lifetime of
the process. The old `_schedule_runner_loop` (subprocess-based 60s polling) has been
superseded by this module.

**Design: adaptive sleep with threading.Event**

The tick thread never polls on a fixed interval. It calculates the exact number of seconds
until the next scheduled fire, then sleeps for that duration using `threading.Event.wait()`.
Any mutation of schedules (add, cancel, complete) calls `_wakeup.set()` immediately,
which interrupts the sleep and recalculates. This is the same pattern used by CPython's
`sched` module and APScheduler's `BackgroundScheduler`. Sleep is capped at 60 s so
the thread stays responsive even when no schedules are pending.

**Thread model:**

```
serve.py main thread
    │
    └── get_manager()
          │
          ├── ScheduleManager._tick_thread   (daemon, named "ScheduleManagerTick")
          │     Loop: _fire_due() → _wakeup.wait(timeout=seconds_until_next)
          │
          └── ThreadPoolExecutor (up to 8 workers, named "ScheduleWorker-N")
                Each worker: _run_schedule() → _execute() → run_loop()
```

**`schedule_loop(config)` — primary entry point:**

```python
from search_scripts.schedule_manager import get_manager
sched_id = get_manager().schedule_loop({
    "primary_prompt":          "...",
    "nextLoop_prompt_static":  "...",
    "nextLoop_prompt_dynamic": ["keyword: X, location: Y", ...],
    "now":               True,   # fire immediately
    "hour_utc":          14,     # or timed: next occurrence of 14:00 UTC
    "minute_utc":        30,     # UTC minute (default 0)
    "repeat":            0,      # 0=once, -1=forever, N=N additional runs after first
    "allow_reschedule":  True,   # auto-reschedule on session/API limit
    "session_threshold": 80.0,
    "weekly_threshold":  80.0,
    "context_threshold": 90.0,
})
```

`now=True` sets `next_run` to the current UTC time. The tick thread fires within ≤1 s.
`now=False` requires `hour_utc`; `next_run` is set to the next future occurrence of
`hour_utc:minute_utc` UTC (today if still in the future, tomorrow if already past).

**`runs_remaining` / repeat semantics:**

| `repeat` | `runs_remaining` | Total runs |
|---|---|---|
| 0 | 0 | 1 (run once, then completed) |
| 3 | 3 | 4 (first + 3 additional) |
| -1 | -1 | ∞ (forever, until cancelled) |

**Limit handling:**

| Condition | `allow_reschedule` | Outcome |
|---|---|---|
| Session/API limit (`limit=1` or `3`) + `reschedule_time` set | `True` | `status=active`, `next_run=reschedule_time` |
| Session/API limit + no `reschedule_time`, or `allow_reschedule=False` | either | `status=limit_reached`; `remaining_loop_prompts` written to entry |
| Weekly limit (`limit=2`) | either | `status=limit_reached`; no auto-reschedule (no `reschedule_time` ever) |

When status is `limit_reached`, `remaining_loop_prompts` and `session_id` are preserved
in the `schedules.json` entry. The dashboard can surface a "Resume" button that passes
those fields back as a new schedule with `continuing_from_limit_reached=True`.

**Session ID recovery:**

`run_loop()` does not return `session_id` in its result dict. Instead, `_execute()` reads
the last record from `runs.json` immediately after `run_loop()` returns (it is synchronous,
so all records are written before it returns). The recovered `session_id` is saved back to
the schedule entry so the next run can pass `--resume <session_id>` to Claude.

**`build_loop_config()` — prompt construction layer (`serve.py`):**

Before reaching `schedule_loop()`, the UI's queue selection must be translated into prompts.
`build_loop_config(queue_ids, settings)` in `serve.py`:
1. Reads `search_queue.json` and filters to the selected entry IDs.
2. Builds one `nextLoop_prompt_dynamic` entry per selected entry:
   `"keyword: <keyword>, location: <location>"`
3. Reads `workflow.md` as `primary_prompt` (or uses `settings["primary_prompt"]` if provided).
4. Returns a merged dict ready for `schedule_loop()`.

**Public API:**

| Method | Called by | Description |
|---|---|---|
| `schedule_loop(config) → str` | `POST /schedule-loop` | Create schedule entry; returns UUID |
| `run_schedule_now(sched_id) → bool` | `POST /run-schedule-now` | Fire existing schedule immediately |
| `cancel_schedule(sched_id) → bool` | `POST /cancel-schedule`, `POST /stop-loop` | Remove idle or mark running for cancellation |
| `get_schedules() → list` | `GET /schedules` | Returns entries annotated with `_is_running`, `_remaining_count` |
| `shutdown()` | `serve.py` `KeyboardInterrupt` | Stops tick thread, shuts down executor |

**Cancellation semantics:** When `cancel_schedule()` is called on a running schedule, it
adds the id to `_cancelled` and immediately calls `terminate_active()` from `claude_call.py`
to kill the Claude subprocess. This causes `run_loop()` to unblock and return quickly; the
worker then sees the id in `_cancelled`, discards the result, and removes the entry.
Idle (not-yet-running) schedules are removed from `schedules.json` without subprocess interaction.

**Input:** `schedules.json` (read/written under lock), `runs.json` (read for session recovery)
**Output:** updated `schedules.json` + `loop_state.json` status updates during execution

---

## 4. Web Dashboard / UI Elements

The dashboard is a single-page app served by `serve.py` across three files:

| File | Lines | Role |
|---|---|---|
| `dashboard.html` | ~1 607 | Shell, CSS, Jobs/Tags/Apply/Settings tab HTML, shared JS (scoring, data loading, batch poll, updateRunBanner/Tab, showTab) |
| `tabs/tab-run.html` | ~214 | Run tab inner HTML — injected lazily by `showTab('run')` on first open |
| `tabs/tab-run.js` | ~1 000 | All Run-tab JS (queue selection, loop/schedule, history, log); loaded eagerly via `<script defer>` |

`serve.py` routes `/tabs/` requests to the project-root `tabs/` directory.
`showTab('run')` fetches `/tabs/tab-run.html` once (guarded by `_runTabLoaded`), injects
it into `#tab-run`, then runs the normal Run-tab initialization sequence.
`loadData()` is called via `DOMContentLoaded` so deferred tab-run.js is always loaded first.

All data flows through the JSON REST API (no WebSocket; polls on a timer).

### 4a. Job Listings Panel

**What it shows:**
- All jobs from `jobs.json`, sorted by computed score (descending)
- Score = base_score (50) + sum of applied tag weights
- Tier badges (Tier 1 / 2 / 3) by percentile rank across the current job pool
- Tag chips color-coded by category (skills=green, avoid=red, location=blue, etc.)
- Per-job `matched_skills`, `red_flags`, `highlights`, `summary` (written by Claude)
- Application status dropdown (saved to `status.json`)
- Smart Apply button

**Input:** `GET /jobs` → `jobs.json`, `GET /tags` → `tags.json`

**Interactions:**
- Status dropdown → `POST /status` (saves to `status.json`)
- Smart Apply → `POST /smart-apply` (creates application folder, copies resume/CL, opens VS Code)

---

### 4b. Batch Runner Panel

Controls single-run execution of a one-off Claude batch.
(`test_claude_call.py` and `batch_loop.py` have been removed; the scheduler
via `ScheduleManager` is the preferred way to run searches.)

**What it shows:**
- Current status: idle / running / done / error / rate_limited
- Live log tail auto-refreshed every 3 s while running
- ↩ Continue button when status is rate_limited or error and a session_id exists

**Input:** `GET /run-status` → `run_state.json` (reconciled, stale PIDs cleaned up)

**Interactions:**
- Run Batch → `POST /run-batch`
- Stop → `POST /run-stop` (kills PID)
- Continue → `POST /continue-run` (resumes with saved session_id)
- Dismiss → `POST /dismiss-run` (hides run from history)

---

### 4c. Automatic Runs Panel (Loop & Schedule)

Controls for launching and managing scheduled `run_loop()` calls via `ScheduleManager`.
All schedule actions go through `schedule_manager.py` in-process — no subprocesses.

**Backend and UI: fully implemented**

All server-side endpoints are live and wired to `ScheduleManager`.
The dashboard UI is fully wired to the new `schedule_manager.py`-based API.

| Endpoint | Handler |
|---|---|
| `POST /schedule-loop` | `build_loop_config()` → `get_manager().schedule_loop()` |
| `POST /stop-loop` | `get_manager().cancel_schedule(active_sched_id)` |
| `POST /cancel-schedule` | `get_manager().cancel_schedule(id)` |
| `POST /run-schedule-now` | `get_manager().run_schedule_now(id)` |
| `GET /schedules` | `get_manager().get_schedules()` — annotated with `_is_running`, `_remaining_count` |
| `GET /loop-status` | reads `loop_state.json`; reconciles stale "running" state via `_is_running` |

**POST /schedule-loop — body:**

```json
{
  "queue_ids":           [1, 3, 5],
  "now":                 true,
  "hour_utc":            14,
  "minute_utc":          0,
  "repeat":              0,
  "repeat_pattern":      "daily",
  "weekday":             0,
  "allow_reschedule":    false,
  "session_threshold":   80.0,
  "weekly_threshold":    60.0,
  "context_threshold":   90.0,
  "primary_prompt_file": "workflow.md"
}
```

`queue_ids` required (400 if absent). `now=true` and `hour_utc` can coexist
(fires immediately and also creates a recurring schedule).

**UI controls (all implemented):**

| Control | Description |
|---|---|
| Queue Selection — Auto | "Run next N" stepper; syncs `batch_size` to server via `POST /queue-settings`; shows stats line (total / pending / due / skipped) and a grouped preview of which entries will run (NEW vs DUE badges) |
| Queue Selection — Manual | Checkbox list with keyword/location filter; All/None buttons |
| Queue Selection — Reorder | Drag-and-drop full queue; skip / re-queue / remove per entry; replaces the old standalone Queue Manager card |
| Primary Prompt | Dropdown populated dynamically from `GET /list-md-files`; shows server-offline / no-files / error states instead of falling back to a hardcoded option |
| When to Run | Radio: Run Now / Scheduled; shows time + repeat-pattern panel when Scheduled |
| Scheduled time display | Shows local timezone abbreviation (detected via `Intl.DateTimeFormat`); double-click abbreviation to override with a common-IANA-zone picker |
| Session limit % | `session_threshold` input |
| Weekly limit % | `weekly_threshold` input |
| Context reset % | `context_threshold` input |
| Auto-reschedule | Checkbox — `allow_reschedule`; when unchecked, "Max reschedule attempts" row is hidden |
| Max reschedule attempts | `repeat` input (0=once, −1=unlimited); only visible when Auto-reschedule is checked |

**Timezone handling in the UI:**

Scheduled times are stored as UTC (`hour_utc`/`minute_utc` in `schedules.json`).
The Run tab converts using the browser's local timezone (or the user's manual override):
- Input: user enters local time → `_localToUtc(h, m)` → stored as UTC
- Display: stored UTC → `_utcToLocal(h, m)` → shown with TZ abbreviation
- Schedule cards: ISO UTC strings → `_fmtUtcIso(isoStr)` for `last_run` / `next_run`

**Card layout (tab-run):**
1. Automatic Runs — primary control surface; contains Queue Selection, Primary Prompt, When to Run, Limits & Settings, Run Now / Schedule It buttons, Scheduled Loops list
2. Recent Runs — scrollable (max-height ~165 px ≈ 5 rows visible); column headers stay pinned above scroll area
3. Loop Log — raw log tail for the active or last run

**Loop status reconciliation:**

`GET /loop-status` reconciles stale state: if `loop_state.json` says `running` but the
schedule's `_is_running` annotation says false, the endpoint mirrors the schedule's
terminal status (`limit_reached`, `error`, etc.) back to `loop_state.json` rather than
blindly overwriting to `"error"`. This eliminates the false error state that appeared
after a loop finished normally.

`_write_loop_state()` now writes the current `serve.py` PID when `status="running"`,
so any future PID-based fallback checks the correct process.

**Schedule card display:**

Schedule entries include `is_immediate: true` when created via "Run Now". The dashboard
card renders this as `"▶ Running now"` (while running) or `"Ran immediately"` (after
completion) instead of the confusing `"Daily @ 00:00 UTC · once"`.

`_serve_schedules()` uses the `_is_running` annotation from `get_manager().get_schedules()`
to correct any schedule whose stored `status` is still `"running"` after the loop ended —
ensuring the schedule card updates within one 4-second poll cycle.

**Input:** `GET /loop-status` → `loop_state.json`, `GET /schedules` → `schedules.json`

**Interactions:**
- Run Now → `POST /schedule-loop` with `{ queue_ids: [...], now: true, ...settings }`
- Schedule It → `POST /schedule-loop` with `{ queue_ids: [...], hour_utc: N, ...settings }`
- Stop Loop → `POST /stop-loop`
- Cancel Schedule → `POST /cancel-schedule` `{ id }`
- Run Schedule Now → `POST /run-schedule-now` `{ id }`
- Resume after limit → `POST /schedule-loop` `{ remaining_loop_prompts: [...], session_id: "...", continuing_from_limit_reached: true, now: true }`
- Return to Queue → `POST /return-remaining-to-queue` `{ id }` (resets remaining prompts to pending)
- Dismiss error/limit → `POST /dismiss-loop-error`

---

### 4d. Queue Manager

The Queue Manager is integrated into the Automatic Runs card as the **Reorder** mode of
Queue Selection (see §4c). There is no longer a separate Queue Manager card.

**Queue endpoints (unchanged, still used by the Reorder panel):**

| Endpoint | Description |
|---|---|
| `GET /queue-manage` | Annotated queue with `_days_since` and `_due` fields |
| `GET /queue-preview` | Batch selection simulation; respects `batch_size` from `search_queue.json` settings |
| `POST /queue-reorder` | Update `order` field on all entries |
| `POST /queue-toggle-skip` | Flip `skip_next` for one entry |
| `POST /queue-remove` | Remove an entry |
| `POST /queue-set-status` | Force `pending` or `done` |
| `POST /queue-settings` | Update `batch_size` and/or `rerun_after_days` |

`batch_size` is the authoritative source for how many entries `GET /queue-preview` returns.
The Run Now stepper (`qsN`) writes back to `batch_size` via `POST /queue-settings` on every
change, and reads the current `batch_size` from the `/queue-preview` response on load to
stay in sync.

---

### 4e. Usage Monitoring Panel

Shows live Claude API cost metrics pulled from cmonitor.

**What it shows:**
- Current session cost and % of plan limit
- 7-day rolling cost and % of plan limit
- Configured plan, thresholds, and polling interval from `usage_limits.json`
- Historical session/weekly charts

**Input:** `GET /usage` → real-time from `pull_usage_data.get_usage(hours_back=168)`
          `GET /usage-limits` → parsed `usage_limits.json`

**Interactions:**
- Save settings → `POST /usage-settings` (writes `usage_limits.json` in place,
  preserving comment keys starting with `"_"`)

---

### 4f. Tag Management

Displays tag statistics and manages the pending-tag review workflow.

**What it shows:**
- All approved tags with weights, categories, usage counts across jobs
- Pending tags (proposed by Claude during runs) awaiting review

**Pending tag actions:**
- **Approve** → `POST /tags/approve` — sets `status: "approved"`; removes `proposed_reason`
- **Reject** → `POST /tags/reject` — removes tag from `tags.json` entirely
- **Merge** → `POST /tags/merge` — merges source into target, adds source as alias, updates all jobs

**Edit tag fields** → `POST /tags/update` accepts:
`weight`, `label`, `description`, `aliases`, `category`, `status`

---

## 5. Session Management

Claude's `--resume <session_id>` continues a conversation across process restarts.
Job Scout uses this to avoid re-sending the full `workflow.md` (~2 KB) every run.

### 5a. Session ID flow through loop_manager / schedule_manager

`loop_manager.py` receives an optional `session_id` at invocation and passes it to
`claude_call.py` as `--resume <session_id>`. After each call, the new `session_id`
returned by the CLI is carried forward to the next loop iteration.

**Session modes:**

| Mode | Condition | Prompt |
|---|---|---|
| New session | `session_id=None` | `primary_prompt + static + dynamic[i]` |
| Resumed | `session_id` set, context OK | `static + dynamic[i]` (workflow already in context) |
| Context reset | `context_pct ≥ context_threshold` | `session_id` cleared; next call uses new-session form |
| Continue from limit | `continuing_from_limit_reached=True`, first call | `continue_prompt` replaces the normal prompt |

**Schedule → session handoff:**

`schedule_manager._execute()` reads the last `runs.json` record after `run_loop()` returns
to recover the `session_id`, then saves it to the `schedules.json` entry so the next
scheduled run can pass `--resume`. `loop_manager` does not return `session_id` directly.

---

### 5b. test_session.json (legacy)

`test_session.json` was used by the now-removed `test_claude_call.py` script to persist
session state between single invocations. It is no longer written or read by any active
code but may still exist on disk from older runs. The `loop_manager` + `schedule_manager`
path manages session state entirely through `runs.json` and `schedules.json`.

---

## 6. Queue Management

`search_queue.json` holds the list of keyword/location pairs to search.

### 6a. Queue Entry Structure

```json
{
  "id": 1,
  "keyword": "mechanical design engineer",
  "location": "Charleston, SC",
  "location_weight": 1.0,
  "status": "pending",
  "last_run": "2026-05-21",
  "order": 0,
  "skip_next": false
}
```

| Field | Meaning |
|---|---|
| `status` | `"pending"` = ready to run; `"done"` = completed this cycle |
| `last_run` | Date of last successful Claude run (YYYY-MM-DD) |
| `order` | Sort position (lower = runs earlier); drag-and-drop in UI |
| `skip_next` | If true, excluded from batch selection until toggled off |

---

### 6b. Batch Selection Logic

`build_loop_config()` in `serve.py` filters to the `queue_ids` sent by the dashboard
(auto mode: first N pending/due entries; manual mode: user-selected entries):
1. Filter: entries matching the selected `queue_ids`, `skip_next != true`
2. Sort by `order` ascending (then `id` as tiebreaker)
3. Build one `nextLoop_prompt_dynamic` entry per selected item

`loop_manager.py` processes dynamic prompts sequentially within one run.
The dashboard `Next Batch Preview` shows which entries would run with the current
`batch_size` and queue state.

---

### 6c. Rerun Logic

Entries with `status=done` are not immediately re-selected.
After `rerun_after_days` (default 14), a done entry becomes "due" and can fill
batch slots if there are fewer pending entries than `batch_size`.

Computed in `/queue-preview`:
```
pending = entries with status=pending and skip_next=false, sorted by order
due     = done entries where (today − last_run) > rerun_after_days
selected = pending[:batch_size] + due[:(batch_size − len(pending))]
```

Rerun ("due") entries are shown with a `_due` annotation in the queue manager.
`batch_size` controls how many entries the dashboard preview shows as "next up",
though in practice each Claude run processes exactly one entry.

---

## 7. Data Files Reference

| File | Written by | Read by | Description |
|---|---|---|---|
| `jobs.json` | Claude | Dashboard, claude | All discovered job listings with tags, scores, summaries |
| `tags.json` | Claude, UI | Claude, Dashboard | Tag registry: weights, categories, aliases, status |
| `status.json` | UI | Dashboard | Per-job application status (saved, applied, interviewing, etc.) |
| `search_queue.json` | UI, `loop_manager.py` | Claude, Python, Dashboard | Queue entries: keyword/location pairs, run status, batch_size setting. `loop_manager` writes `status="done"` and `last_run` after each successful iteration. |
| `usage_limits.json` | UI | Dashboard | Plan limits, watchdog thresholds, loop configuration |
| `run_state.json` | `test_claude_call.py` | `serve.py`, Dashboard | Current/last single-run status: status, pid, resume_at, error |
| `loop_state.json` | `schedule_manager.py` | `serve.py`, Dashboard | Current schedule execution status: status, active_sched_id, pid (current serve.py PID when running), updated; written before and after each `run_loop()` call |
| `test_session.json` | `test_claude_call.py` | `test_claude_call.py` | Claude session ID, paused flag, paused_entry |
| `schedules.json` | `schedule_manager.py` | `serve.py`, Dashboard | All schedule entries with full state: status, next_run, session_id, remaining_loop_prompts, runs_remaining, last_result, settings, is_immediate |
| `runs.json` | `loop_manager.py` | `schedule_manager.py`, Dashboard | Historical record of every Claude call: timestamps, status, costs, job delta, session_id (used by schedule_manager to recover session_id after each run) |
| `smart_apply_config.json` | UI | UI | Smart Apply default paths and application history |
| `logs/loop_log_<ts>.txt` | `loop_manager.py` | Admin | Orchestration log for one run: thresholds, modes, per-loop summary |
| `logs/loop_log_<ts>_claude_<n>.txt` | `claude_call.py` | Admin | Full stream-json transcript for the nth Claude call in that run |
| `profile.md` | UI | Claude | Seth's skills, background, and target industries |
| `workflow.md` | (static) | `loop_manager.py`, `serve.py` | Claude's job-search instructions; embedded as `primary_prompt` on new sessions |
| `tabs/tab-run.html` | (static) | Browser (lazy) | Run tab inner HTML; served at `/tabs/tab-run.html` and injected into `#tab-run` on first tab open |
| `tabs/tab-run.js` | (static) | Browser (deferred) | All Run-tab JavaScript; served at `/tabs/tab-run.js` and loaded with `<script defer>` |
