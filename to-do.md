# Job Scout — Development To-Do

All items below are either implemented, in progress, or pending.
Status: ✅ Done | 🔄 In Progress | ⬜ Pending

---

## #1 — Manual City/Location Selection for Next Batch ✅ IMPLEMENTED

**Goal:** Let the user pick exactly which queue entries run in the next batch from the UI, instead of relying solely on "pending" or "due" auto-selection.

**Implementation:**
- Add `skip_next: true` flag to individual queue entries in `search_queue.json`
- New GET `/queue-manage` endpoint returns full queue with status + skip_next flags
- New POST `/queue-toggle-skip` endpoint flips `skip_next` on a single entry
- New POST `/queue-toggle-enable` endpoint re-enables a `done` entry (sets it back to `pending` so it runs next)
- Run tab → "Next Batch Preview" panel expanded to show full queue with checkboxes
- Queue preview logic in `serve.py::_serve_queue_preview` respects `skip_next` flag
- Same logic mirrored in `run_batch.py::describe_batch()` and `workflow.md` Step 1

**Files changed:** `serve.py`, `dashboard.html`, `run_batch.py`, `workflow.md`

---

## #2 — Skip or Remove Next Batch Entry ✅ IMPLEMENTED

**Goal:** Complement to #1 — let the user explicitly skip (temp) or remove (permanent) queue entries.

**Implementation:**
- "Skip next" toggle in queue management UI (sets `skip_next: true`, reverts after run)
- "Remove" button permanently deletes entry from `search_queue.json`
- API: POST `/queue-remove` deletes entry by id
- Skipped entries are visually distinct in the preview; removed entries are gone
- After a batch runs, `skip_next` flags are cleared on all run entries

**Files changed:** `serve.py`, `dashboard.html`, `search_queue.json` (data)

---

## #3 — Usage Threshold Tracking, Graceful Exit, Auto-Resume ✅ IMPLEMENTED

**Goal:** Ensure Claude sessions stop cleanly when hitting plan limits and resume automatically after the cooldown window.

**Implementation (already partially done in `test_claude_call.py`):**
- `UsageWatchdog` polls claude-monitor every `check_interval_seconds`
- At `warn_at_pct` (85%) of session or weekly cost → logs warning to run log
- At 100% → terminates Claude subprocess, writes `run_state.json { status: "rate_limited", resume_at }`
- `serve.py::_auto_restart_loop` daemon thread checks every 30s; re-launches when `resume_at` passes
- Cooldown gate at start of `test_claude_call.py` prevents re-entry during cooldown window

**Tests to add:**
- `scripts/test_watchdog.py`: mock proc + mock monitor → verify watchdog fires at 100%
- Manual test: set `session_cost_usd: 0.01` in `usage_limits.json`, run batch, verify graceful stop

**Files:** `test_claude_call.py` (done), `usage_limits.json` (done)

---

## #4 — Sequential Batch Loop with Configurable Stop Conditions ✅ IMPLEMENTED

**Goal:** Automatically run multiple batches end-to-end with configurable stop conditions, including scheduled nightly runs.

**Stop conditions (all configurable in `usage_limits.json`):**
- `max_batches_per_session`: stop after N batch entries (e.g., 5)
- `max_runs_per_loop`: stop after N full batch runs
- `schedule_hour_utc`: if set, only run during a specific hour UTC (nightly window)
- `weekly_cost_threshold_pct`: stop when weekly usage exceeds this fraction of limit (e.g., 0.6 = 60%)

**Implementation:**
- `test_claude_call.py` already loops via serve.py auto-restart; formalize this
- New `scripts/batch_loop.py` script that runs `test_claude_call.py` in a Python loop
  (rather than relying on serve.py restart thread) for more precise control
- `batch_loop.py` checks stop conditions between each iteration
- Dashboard shows loop status + stop condition progress

**Files:** new `scripts/batch_loop.py`, `usage_limits.json` (new fields), `dashboard.html`, `serve.py`

---

## #5 — One Batch Entry Per Claude Session ✅ IMPLEMENTED

**Goal:** Each Claude invocation processes exactly one queue entry. This creates a clean exit point after each entry — if insufficient usage budget remains, remaining entries are deferred cleanly.

**Implementation (already done in `test_claude_call.py`):**
- `test_claude_call.py` gets first pending entry → builds a prompt with workflow.md + that single entry
- Session ID is saved after each run; next run resumes the same session (if valid)
- If context limit or watchdog fires → session invalidated → next run starts fresh session
- `run_batch.py` (production) still sends all batch_size entries at once — this is a known gap

**To finalize:**
- Set `TEST_MODE = False` in `serve.py` once `test_claude_call.py` is production-ready
- OR update `run_batch.py` to call `test_claude_call.py` logic in a loop (preferred for #4)
- Update `workflow.md` Step 1 to acknowledge single-entry override

**Files:** `serve.py` (TEST_MODE flag), `run_batch.py`, `test_claude_call.py`

---

## #6 — Configurable UI for Usage Thresholds and Batch Settings ✅ IMPLEMENTED

**Goal:** The user should be able to see and adjust all usage/scheduling parameters from the dashboard without editing JSON files.

**Implementation:**
- Settings tab: new "Usage & Scheduling" section with form fields for all `usage_limits.json` values
- New POST `/usage-settings` endpoint saves changes to `usage_limits.json`
- Live usage meter: progress bars for current session cost and weekly cost vs. limits
- Shows next `resume_at` time when in `rate_limited` state
- Shows batch loop status (current entry count, total run count, weekly cost %)
- Run tab: existing batch_size / rerun_after_days steppers (already implemented)

**Fields to expose in UI:**
- Plan selector (pro / max5 / max20 / custom)
- Session cost limit (USD) — with plan default shown
- Weekly cost limit (USD) — with plan default shown
- Check interval (seconds)
- Cooldown delay (hours)
- Warn threshold (%)
- Max batches per session
- Max runs per loop
- Schedule hour UTC (nightly window)
- Weekly cost threshold % (stop loop)

**Files:** `serve.py` (new endpoint), `dashboard.html` (Settings tab), `usage_limits.json` (new fields)

---

## #7 — Optimize Matching: Pass-Gate Pipeline + Comparison Testing ✅ IMPLEMENTED

**Goal:** Reduce token cost while maintaining ranking quality. Implement tiered filtering so heavy
LLM resources are only applied to plausible candidates.

### Method 1 — Pass-Gate Pipeline (suggested)

Replace current workflow.md all-at-once tagging with a staged filter:

1. **Search** → raw job list from Indeed
2. **Title filter** (already exists) → drop senior/lead/manager/etc.
3. **Dedup** (already exists) → skip fingerprint matches
4. **Keyword gate (local)** → run a cheap keyword match on title + snippet (no LLM)
   - Must match ≥1 of: solidworks, mechanical, design, product, robotics, additive, prototype
   - Eliminates obvious non-matches before fetching JD
5. **Fetch JD** → `get_job_details()` only for survivors
6. **Tag extraction (LLM)** → create JD tags: required skills, years exp, domain keywords
   - Stripped-down prompt: "List required skills and qualifications as JSON tags"
   - Much cheaper than full tagging
7. **Minimum requirements gate** → eliminate jobs that fail hard requirements:
   - Required exp > 3 years → skip
   - Missing all of: solidworks / CAD / 3D mentioned → soft skip
8. **Full profile match (LLM)** → survivors get full tag scoring vs. profile
9. **Track eliminated jobs** → store with reason in jobs.json (for dedup on future runs)

### Method 2 — Sentiment / Keyword Analysis (local compute)

Use Python-only NLP (no LLM API cost):
- `TF-IDF` or `sklearn` cosine similarity between job description and Seth's profile keywords
- `spaCy` NER to extract years of experience mentioned
- Keyword density scoring against a curated skills vocabulary
- Output: a local pre-score (0–1) used as an additional pass gate before LLM tagging

**Candidate libraries:** `scikit-learn` (TF-IDF), `spaCy` (NER), NLTK (basic), or pure regex

### Testing Methodology (side-by-side comparison)

1. Save a reference dataset: `test_jobs_sample.json` (N=20+ diverse jobs, pre-fetched JDs)
2. For each method, run tagging on the same dataset and record:
   - Tokens used (input + output)
   - Cost (USD)
   - Tags assigned per job
   - Resulting score and tier per job
3. Compare: Tier 1 overlap across methods (Jaccard similarity of top-25% sets)
4. Target: same Tier 1 jobs regardless of method, with lower cost in methods 2+
5. `scripts/compare_methods.py` — CLI tool to run comparison and print report

**Files:** new `scripts/batch_loop.py`, `scripts/compare_methods.py`, `workflow_v2.md`, `workflow_v3.md`, `test_jobs_sample.json`

---

## #8 — Smart Apply Tab ✅ IMPLEMENTED

**Goal:** One-click job application setup: creates a folder, copies resume/cover letter,
opens Claude Code in the folder with the resume-tailor skill pre-loaded.

### UI

- New tab "Apply" (between Tags and Run)
- Job cards get a functional "Smart Apply" button (currently shown but disabled)
- Tab shows recently created application folders

### Flow

1. User clicks "Smart Apply" on a job card
2. Dialog opens with:
   - Application name (pre-filled: `<Company> — <Job Title>`)
   - Destination parent folder (default: user-configurable, persisted in `smart_apply_config.json`)
   - Resume source (default path, persisted)
   - Cover letter source (default path, persisted)
3. User confirms → POST `/smart-apply`
4. Server:
   a. Creates folder: `<parent>/<Company> — <Job Title>/`
   b. Writes `JD.txt` with full job description (fetched if not cached)
   c. Copies resume and cover letter into folder
   d. Writes the deep-link file or launches VS Code directly:
      ```
      code --folder-uri "vscode://file/<folder>" "<folder>"
      ```
   e. Optionally pre-fills Claude Code with resume-tailor prompt via deep link
5. Folder is tracked in `smart_apply_config.json` application history

### Resume-tailor skill reminder

The `resume-tailor` skill should be updated to remind the user:
> "I will generate new edited copies of your resume and cover letter on first pass.
> After that initial edit, I will only suggest changes in chat unless you ask me to
> regenerate. Let me know when you want to proceed."

### Configuration stored in `smart_apply_config.json`

```json
{
  "default_parent_folder": "C:/Users/Seth/...",
  "default_resume_path":   "C:/Users/Seth/.../resume.docx",
  "default_cover_letter_path": "C:/Users/Seth/.../cover_letter.docx",
  "applications": [
    { "job_id": "...", "company": "...", "title": "...", "folder": "...", "created": "date" }
  ]
}
```

**Files:** `serve.py` (new endpoints), `dashboard.html` (new tab + button), new `smart_apply_config.json`

---

## Priority Order

| Priority | Item | Complexity | Notes |
|----------|------|------------|-------|
| 1 | #1 + #2 | Medium | Immediate user control value |
| 2 | #6 | Medium | Makes #3/#4/#5 user-friendly |
| 3 | #5 | Low | TEST_MODE flip + validation |
| 4 | #3 | Low | Already implemented; add tests |
| 5 | #4 | Medium | Needs batch_loop.py |
| 6 | #8 | High | New tab, file ops, deep link |
| 7 | #7 | High | Research + multi-method testing |
