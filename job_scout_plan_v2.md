# Job Scout — Plan v2 (Claude + Indeed MCP)

**Runtime:** Claude (via Claude Code headless) + existing Indeed MCP
**Storage:** JSON files
**Scheduling:** cron → `claude -p`
**UI:** HTML dashboard, optionally with tiny local server

No custom APIs, no source plugins, no LLM glue code. Claude is the engine.

---

## Architecture

```
┌──────────┐    invokes     ┌─────────────────────┐
│   cron   │ ─────────────▶ │  claude -p (Code)   │
└──────────┘                │  reads workflow.md  │
                            │  ↓                  │
                            │  Indeed MCP search  │
                            │  Indeed MCP details │
                            │  rank against       │
                            │   profile.md        │
                            │  ↓                  │
                            │  writes JSON files  │
                            └─────────────────────┘
                                     │
                                     ▼
                            ┌─────────────────────┐
                            │  dashboard.html     │
                            │  reads jobs.json    │
                            │  writes status.json │
                            └─────────────────────┘
```

---

## File layout

```
job-scout/
├── profile.md           # Plain-English fit criteria (Claude reads)
├── workflow.md          # Step-by-step Claude follows each run
├── search_queue.json    # Location × keyword matrix + status
├── jobs.json            # Every job found, with ranking
├── status.json          # Applied / dismissed / interested flags
├── runs.json            # Run history
├── dashboard.html       # Local UI
└── scripts/
    ├── run.sh           # Cron entrypoint
    └── serve.py         # Optional dashboard server (~30 lines)
```

---

## Data schemas

### profile.md
Plain markdown. Skills, target role, locations with weights, avoid list, hard filters. Claude reads it as scoring context.

### search_queue.json
```json
{
  "settings": {
    "batch_size": 10,
    "rerun_after_days": 14
  },
  "queue": [
    {
      "id": 1,
      "location": "Charleston, SC",
      "keyword": "mechanical design engineer",
      "status": "done",
      "last_run": "2026-05-20"
    },
    {
      "id": 2,
      "location": "Charleston, SC",
      "keyword": "product design engineer",
      "status": "pending",
      "last_run": null
    }
  ]
}
```

### jobs.json
```json
{
  "jobs": [
    {
      "indeed_id": "JOB_77",
      "fingerprint": "abcosubsea|entrylevelmechanicaldesignengineer|houstontx",
      "title": "Entry Level Mechanical Design Engineer",
      "company": "ABCO Subsea, LLC",
      "location": "Houston, TX",
      "salary": "$70,000 - $95,000",
      "url": "https://to.indeed.com/aa9h4rm2klhy",
      "posted": "2026-03-27",
      "first_seen": "2026-05-20",
      "last_seen": "2026-05-20",
      "score": 72,
      "tier": "tier_2",
      "matched_skills": ["SolidWorks-adjacent", "design ownership"],
      "red_flags": ["prefers Autodesk Inventor not SolidWorks", "subsea O&G niche"],
      "highlights": ["entry-level title", "EIT preferred (Seth has FE)"],
      "summary": "Entry-level design role on Gulf coast. Inventor preference is the main mismatch.",
      "search_id": 47
    }
  ]
}
```

Fingerprint dedupes the same job across multiple searches.

### status.json
```json
{
  "JOB_77": {"status": "interested", "updated": "2026-05-21", "notes": ""},
  "JOB_88": {"status": "dismissed", "updated": "2026-05-21", "reason": "5+ years required"}
}
```

Kept separate from `jobs.json` so re-runs can rebuild `jobs.json` without losing your manual flags.

### runs.json
```json
{
  "runs": [
    {
      "started": "2026-05-21T03:00:00",
      "ended": "2026-05-21T03:04:12",
      "searches_run": 10,
      "jobs_seen": 87,
      "jobs_new": 14,
      "jobs_scored": 14,
      "errors": []
    }
  ]
}
```

---

## workflow.md (the instructions Claude executes each run)

Roughly:

```
You are running a scheduled job search for Seth. Follow these steps exactly. 
Do not produce a chat summary — your output is the modified JSON files.

1. Read profile.md, search_queue.json, jobs.json, status.json, runs.json.

2. Pick next batch from search_queue:
   - First: any "pending" searches, in order.
   - Then if room: "done" searches where last_run > rerun_after_days ago.
   - Limit to settings.batch_size total.

3. For each picked search:
   a. Call Indeed:search_jobs(country_code="US", location, search, job_type="fulltime")
   b. For each returned posting:
      - fingerprint = slug(company) + "|" + slug(title) + "|" + slug(location)
      - If fingerprint exists in jobs.json: update last_seen, skip to next.
      - Else: tentatively add with first_seen=today.
   c. For each NEW job whose title looks promising 
      (mechanical/design/engineer/product, NOT senior/lead/manager/principal):
      - Call Indeed:get_job_details(job_id)
      - Score 0-100 against profile.md. Required experience > profile cap = auto <50.
      - Assign tier: >=80 tier_1, 60-79 tier_2, <60 tier_3.
      - Fill matched_skills, red_flags, highlights, summary.
   d. Mark search id as status:done, last_run:today in search_queue.json.

4. Append run record to runs.json.

5. Save all modified JSON files. Stop.
```

This file is the "code." Edit it to change behavior — no Python changes needed.

---

## Scheduling

**scripts/run.sh:**
```bash
#!/usr/bin/env bash
cd "$(dirname "$0")/.."
claude -p "$(cat workflow.md)" --output-format text >> logs/runs.log 2>&1
```

**cron (macOS/Linux):**
```
0 3 * * * /Users/seth/code/job-scout/scripts/run.sh
```

For macOS sleep-resilience, use launchd instead of cron — runs even if the machine wakes from sleep at the scheduled time.

---

## Dashboard

`dashboard.html` — single file, vanilla JS or React via CDN.

**On load:**
- `fetch('./jobs.json')` and `fetch('./status.json')`
- Filter out `status=dismissed`
- Sort by score desc, group by tier
- Render each job: title, company, location, score, summary, matched skills, red flags, Apply button (opens Indeed URL)

**Actions per job:**
- "Mark applied" → POST to local server (or localStorage)
- "Dismiss" → POST to local server
- "Interested" → flag for follow-up

**Two implementation options:**

**A. localStorage only (simplest):**
- Status changes saved to browser localStorage
- Export button writes status.json to Downloads, you manually move it back
- Pros: zero infra. Cons: status doesn't sync to Claude's next run automatically.

**B. Tiny Python server (recommended):**
```python
# serve.py — ~30 lines FastAPI
# GET  /jobs    → returns jobs.json content
# GET  /status  → returns status.json content
# POST /status  → writes single entry to status.json
# Static serve dashboard.html
```
Run `python serve.py`, open `localhost:8000`. Claude's next run reads the latest status.json and skips/deprioritizes dismissed jobs.

---

## profile.md — example structure

```markdown
# Seth — Job Search Profile

## Target role
Entry-level mechanical design / product design / R&D engineer.
Max 3 years required experience. Not applications, sales, or field service.

## Skills
- Strong: SolidWorks, FDM/3D printing, tolerance analysis, mechanism design, Python, hands-on prototyping
- Proficient: FEA, GD&T, electronics/Arduino, CNC
- Exposure: Creo, Inventor

## Location preferences (weighted)
Must be <3hr drive from a US coast.
- 1.0 — Charleston, SC area
- 0.9 — coastal Florida
- 0.8 — Boston / New England coast
- 0.7 — Pacific Northwest / Mid-Atlantic
- 0.6 — California coastal, Texas Gulf

## Industries
- Interested: medical devices, robotics/automation, additive manufacturing, marine, consumer products, aerospace
- Avoid: pure HVAC, applications engineering, sales engineering

## Hard filters
- Min salary: $65K
- US citizen, no sponsorship needed
- Full-time only
- Skip if "Senior", "Lead", "Manager", "Principal" in title

## Notes for scoring
- 4 years project management experience in commercial IT — treat as transferable for cross-functional collaboration, not as mechanical engineering experience
- Mature self-developed design process is a real differentiator for any role mentioning "ownership" or "self-directed"
- Maker and project work as well
- protfolio at sethemond.com
```

You edit this file when your priorities shift. Claude reads it fresh each run.

---

## Open decisions (need to confirm before building)

1. **Indeed MCP in Claude Code.** The Indeed MCP currently authenticated through Claude.ai needs to also be configured in Claude Code's MCP config (`~/.claude.json` or `.mcp.json` in project). **Verify this works first** — it's the gating dependency. If the Indeed MCP can't be reached from Claude Code, the whole plan needs rethinking (fallback: scheduled web scrape, or running in Claude.ai API mode).

2. **Dashboard: localStorage vs. tiny server.** Recommend tiny server (Option B) — barely more work, status syncs back to Claude.

3. **Batch size per run.** Start with 10 searches. Each search returns up to 10 results, so ~100 results, maybe 10-20 worth pulling details on. Total run time ~5 min.

4. **Re-run interval.** 14 days seems right for a given location × keyword combo. Configurable in search_queue.json.

5. **Search matrix size.** Start with maybe 5 keywords × 30 locations = 150 combos. With batch_size=10 and daily runs, full coverage every 15 days.

---

## First steps in Claude Code

In an empty `job-scout/` directory:

1. **Verify Indeed MCP works.** Test one search command in Claude Code. If it fails, stop and debug auth.
2. **Scaffold files.** Create profile.md, workflow.md, empty JSON files, the search_queue.json seeded from the tracker matrix we already built.
3. **Test one batch manually.** Run `claude -p "$(cat workflow.md)"` and verify the JSON files update correctly.
4. **Build dashboard.html** + decide on server option.
5. **Schedule.** Add cron/launchd entry.

Each step is small and testable. No big-bang build.
