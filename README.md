# Job Scout

An automated job search agent that uses Claude AI and the Indeed MCP to search, evaluate, and rank job listings — so I'm not manually clicking through the same handful of cities every week.

Note: I'm made this because I have very broad job search and location parameters that work for me.


---

> **Status:** First fully working prototype. Running locally on my machine only — no effort has been made yet to make this portable to other environments. Expect rough edges and future changes.

---

## Why This Exists

Job searching across multiple locations is repetitive work with a clear pattern: search a keyword in a city, skim the results, decide what's worth reading, repeat for the next city. Multiply that by a dozen search terms and many locations and you've burned an afternoon doing something a computer should do.

The obvious move is to automate the searching and initial filtering. The less obvious move — and the part I'm more interested in — is what happens when the task is too long to fit in a single AI session.

Claude has a session cost limit. A full search sweep across a large queue takes multiple sessions. That means the automation needs to:

- detect when it's approaching a limit *before* hitting it
- stop cleanly and record exactly where it left off
- resume from that point in the next session without re-doing work

That's the foundation of what I'm calling **long-horizon task automation**: giving an AI agent structured enough state management that it can pause, come back, and continue as if nothing happened. Job Scout is the first concrete project where I've actually built and tested this pattern end-to-end. The methodology here is meant to generalize — the job search is the use case, but the loop-and-resume infrastructure is the interesting part.

---

## The Looping Methodology

This is the core of the project. Everything else is scaffolding around it.

### The problem with long AI tasks

Most AI interactions are stateless: you send a prompt, you get a response, done. But a real task — searching 20 keyword/location combinations, fetching full job descriptions, evaluating each one against a profile — doesn't fit in a single call. It might not even fit in a single Claude *session* before hitting cost limits.

The naive solution is to just run it in chunks manually. The better solution is to build a loop that manages all of that automatically.

### How the loop works

```
Schedule fires (or user clicks "Run Now")
    │
    ▼
loop_manager.py — iterates over a list of dynamic prompts (one per queue entry)
    │
    │  Before each Claude call:
    ├── Check session cost %  →  stop if above threshold (e.g. 80%)
    ├── Check weekly cost %   →  stop if above threshold
    ├── Check context size %  →  if too full, reset session_id (start fresh)
    │
    │  Build prompt:
    ├── New session:      workflow.md  +  static instructions  +  this entry
    └── Resumed session:  static instructions  +  this entry  (workflow already in context)
    │
    ▼
claude_call.py  →  claude -p <prompt> --resume <session_id> --output-format stream-json
    │
    │  Streams response events, parsing:
    ├── session_id (captured from first event)
    ├── Claude text output and tool calls
    ├── Final usage stats (input tokens, cost)
    └── Limit signals in result text  →  STATUS_LIMITS
    │
    ▼
After each call:
    ├── Write record to runs.json (timestamp, cost, job delta, session_id, status)
    ├── Mark queue entry done  →  search_queue.json
    └── Pass session_id forward to next iteration (avoiding workflow.md re-send)
```

### What happens at a limit

If Claude returns a limit signal (session cost, context size, or API rate limit), `loop_manager` sets `limit_exceeded` and returns early with the list of **remaining unprocessed prompts**.

`schedule_manager` catches this and writes the remaining prompts and the current `session_id` back to the schedule entry. The schedule status becomes `limit_reached`. From the dashboard, you can:

- **Resume** — fires a new run immediately, picking up from the remaining entries
- **Auto-reschedule** — if enabled, the scheduler calculates the session reset time and fires automatically when it passes

The key insight is that **all state is files**. `runs.json`, `schedules.json`, `loop_state.json`, and `search_queue.json` together contain everything needed to reconstruct exactly where a run stopped. If the server crashes, you lose at most one in-progress Claude call — the next start picks up cleanly.

### Session continuity

Claude's `--resume <session_id>` continues a conversation across process restarts. After each call, the session ID goes into `runs.json`. Before the next call, `schedule_manager` reads the last record back and passes it to `loop_manager`. This means `workflow.md` (the main ~2KB prompt) only gets sent once per session, not once per queue entry — which matters at scale.

If context grows too large (configurable threshold, default 90%), the session resets and the full prompt goes out fresh on the next call.

---

## Run Tab

The Run tab is where you actually drive the automation. It has three main sections:

### Automatic Runs

This is the primary control surface.

**Queue Selection** has three modes:
- **Auto** — pick the next N pending/due entries automatically, with a preview of exactly which ones will run and whether they're new or due for a rerun
- **Manual** — checkbox list with keyword/location filter; pick exactly what you want
- **Reorder** — drag-and-drop the full queue; skip, re-queue, or remove individual entries

**Primary Prompt** — dropdown of available `.md` files in the project; `workflow.md` is the default. The workflow file is what Claude reads at the start of every new session to understand the task.

**When to Run** — Run Now or Scheduled. Scheduled runs show local time with timezone detection and let you set a repeat pattern (daily, weekly, once).

**Limits & Settings** — session cost threshold %, weekly cost threshold %, context reset threshold %, auto-reschedule toggle.

Clicking **Run Now** fires a schedule immediately. The tick thread wakes within ≤1 second and executes.

### Recent Runs

A scrollable history of completed Claude calls. Each row shows timestamp, status, number of new jobs found, cost, and which schedule it came from. The session_id is tracked per run so any row can be resumed.

### Loop Log

Live log tail from the active or most recent run. Auto-refreshes every few seconds while a run is in progress. Shows threshold check results, prompt construction decisions, Claude output summary, and per-call cost.

---

## How the Rest of It Works

```
Browser (dashboard.html + tabs/tab-run.js)
    │  HTTP GET/POST (polling every 4s)
    ▼
serve.py  ──────────────────────────────────────────────
    │
    ├── ScheduleManager (background thread)
    │       Adaptive sleep — wakes at exact second of next scheduled fire
    │       ThreadPoolExecutor — up to 8 concurrent schedules
    │
    └── Per schedule: run_loop()  →  claude_call.py  →  Claude CLI
                                                              │
                                                        Indeed MCP
                                                              │
                                                    jobs.json / tags.json
```

**Claude does the semantic work** — reading job descriptions, applying tags from the registry, writing summaries, flagging red flags. **Python handles everything else** — scheduling, cost tracking, state persistence, the web UI, subprocess management.

### Tagging and scoring

Each job gets a set of tags from `tags.json`. Tags have weights (positive for good signals, negative for bad). The dashboard computes a score in JavaScript at render time — no stored scores, always reflects current tag weights. Jobs are placed into Tier 1/2/3 by percentile rank across the current pool. If Claude encounters a meaningful characteristic with no matching tag, it calls `scripts/tag_create.py` to propose a new one. Proposed tags go into a pending state and appear in the Tags tab for review before becoming permanent.

---

## Project Structure

```
Search Automation/
│
├── scripts/
│   ├── serve.py                  Main server — HTTP API + background scheduler
│   ├── run_batch.py              Subprocess: runs a single Claude batch (non-test mode)
│   ├── pull_usage_data.py        Dynamically loaded for /usage endpoint
│   ├── tag_create.py             Called by Claude during sessions to propose new tags
│   ├── generate_queue.py         Utility: seeds search_queue.json
│   ├── restart_server.py         Utility: restarts serve.py
│   ├── update_profile_prompt.md  Prompt template for profile updates
│   └── dedup_tags_prompt.md      Prompt template for tag deduplication
│
├── search_scripts/               Core loop orchestration (imported by serve.py)
│   ├── schedule_manager.py       Tick thread + thread pool; owns all scheduled runs
│   ├── loop_manager.py           Multi-turn loop: thresholds → prompt → call → record
│   ├── claude_call.py            Subprocess wrapper for the Claude CLI
│   ├── check_claude_usage.py     Polls claude-monitor for session/weekly cost %
│   ├── context_usage.py          Reads context size % from Claude's JSONL session logs
│   └── count_jobs.py             Reads jobs.json to measure job delta per run
│   │
│   └── tests/
│       ├── test_loop_manager.py              87 unit tests (all pass)
│       ├── test_schedule_manager.py          83 unit tests (all pass)
│       └── test_loop_manager_integration.py  Real Claude calls (skipped without binary)
│
├── tabs/
│   ├── tab-run.html              Run tab HTML (lazy-loaded on first open)
│   └── tab-run.js                All Run-tab JavaScript (~1000 lines)
│
├── dashboard.html                Single-page app served by serve.py
├── workflow.md                   Claude's job-search instructions (the primary prompt)
├── profile.md                    Personal background, targets, hard filters — gitignored
│
└── Runtime data files (gitignored, generated):
    jobs.json, tags.json, status.json, search_queue.json,
    run_state.json, loop_state.json, schedules.json, runs.json,
    smart_apply_config.json
```

---

## Setup & Running

> This runs on my machine. The dependencies below need to be installed and working. No setup scripts or containerization exist yet.

### Prerequisites

- **Python 3.14+**
- **Claude CLI** (`claude`) — installed and authenticated
- **Indeed MCP** — configured in Claude's MCP settings
- **claude-monitor** (`cmonitor`) — uv tool for reading session cost data
- **Claude Pro / Max plan** — the automation is built around session cost limits

### Run

```bash
python scripts/serve.py
```

Then open `http://localhost:8000`.

### Generate the queue (first time)

Edit `scripts/generate_queue.py` with your keyword + location pairs, then:

```bash
python scripts/generate_queue.py
```

### Run the tests

```bash
python -m pytest search_scripts/tests/test_loop_manager.py
python -m pytest search_scripts/tests/test_schedule_manager.py
```

---

## Dashboard Tabs

| Tab | What it does |
|-----|------|
| **Jobs** | Ranked job listing — tag chips, tier badges, score, application status dropdown |
| **Tags** | Tag registry — approve/reject/merge Claude-proposed tags; edit weights |
| **Apply** | Smart Apply: create an application folder, copy resume/CL, open in VS Code |
| **Run** | The main control surface — queue, scheduling, history, live log (see above) |
| **Settings** | Usage limits and plan configuration (partially functional — see below) |

---

## Known Issues & Planned Improvements

### Functionality

1. **Token usage is higher than it needs to be.** Claude handles reading and writing `jobs.json` directly, which burns tokens on file I/O that Python could do. The right fix is a dedicated write script — Claude hands off structured job data, Python handles the file and validates the JSON. Dedup logic should move out of the session too. This is the highest-leverage improvement.

2. **Smart Apply needs testing and debugging.** The flow (create folder → copy resume/CL → open VS Code) is implemented but hasn't been put through real use.

3. **The `resume-tailor` skill output needs work.** Current quality isn't where it needs to be for actual applications.

4. **Jobs page filtering is too basic.** No way to filter by tag, tier, location, or date range — just scroll.

5. **Settings tab is mostly non-functional.** The UI exists but most of the backend wiring for saving and applying settings is incomplete.

6. **Occasional terminal windows pop up** during Claude subprocess calls on Windows. Annoying but not blocking.

7. **Behavior while the computer is asleep is unknown.** The scheduler uses wall-clock time. Whether scheduled runs survive sleep/wake cycles hasn't been tested.

8. **Auto-reschedule after rate limits hasn't been tested end-to-end.** The logic is implemented — if a session limit is hit mid-run, the scheduler saves the remaining entries and reschedules for the next session reset. In practice I have a 5-hour session limit and want Claude available during the day, so only one full session per day goes to job search anyway.

### Things to Explore

9. **Could we use the Indeed API directly instead of MCP?** The MCP round-trip goes through Claude's context window. A direct API call from Python would be cheaper — Claude only sees results, not the overhead.

10. **Local keyword pre-filtering.** A small local model (4B or 8B, via Ollama) as a pass-gate before sending anything to Claude could cut token costs significantly. Cheap local inference screens obvious mismatches; Claude only sees the plausible candidates.

11. **Node-RED or similar for the loop/schedule layer.** The scheduling and looping logic in `schedule_manager.py` and `loop_manager.py` is fairly generic — call this thing, check the result, wait, repeat. A visual flow tool would make that logic easier to modify and reuse for other LLM automation tasks beyond job search.

---

## Related

- Portfolio: [sethemond.com](https://sethemond.com)
