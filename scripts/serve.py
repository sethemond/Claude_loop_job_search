#!/usr/bin/env python3
"""
serve.py — Job Scout dashboard HTTP server
==========================================

PURPOSE
-------
Serves dashboard.html to the browser and provides a JSON REST API that the
dashboard JavaScript uses to read/write all application state.  Also runs two
persistent background threads that fire automated processes on a schedule.

COMPONENT ARCHITECTURE
-----------------------
  Browser (dashboard.html)
    │  GET/POST HTTP
    ▼
  Handler (BaseHTTPRequestHandler)   ← this file
    │
    ├─ Job listings & ranking
    │    GET /jobs, GET /tags, GET /pending-tags
    │    POST /status, /tags/update, /tags/create, /tags/approve,
    │         /tags/reject, /tags/merge, /run-dedup
    │
    ├─ Claude batch runner (single run)
    │    POST /run-batch       → spawns test_claude_call.py
    │    POST /run-stop        → kills running batch
    │    POST /continue-run    → resumes a paused session
    │    GET  /run-status      → run_state.json (reconciled)
    │    GET  /run-log         → tail of test_claude_call.log
    │
    ├─ Claude batch loop (automated multi-run)
    │    POST /run-loop        → alias for /schedule-loop with now=true
    │    POST /schedule-loop   → unified: now=true and/or hour_utc=N
    │    POST /stop-loop       → kills running batch_loop.py
    │    GET  /loop-status     → loop_state.json (reconciled)
    │
    ├─ Schedule management
    │    GET  /schedules       → schedules.json
    │    POST /schedule-loop   → create a timed schedule entry
    │    POST /cancel-schedule → remove a schedule entry
    │    POST /run-schedule-now → fire a schedule entry immediately
    │
    ├─ Queue management
    │    GET  /queue-preview   → which entries would run next
    │    GET  /queue-manage    → full queue with days-since annotation
    │    POST /queue-settings, /queue-reorder, /queue-toggle-skip,
    │         /queue-remove, /queue-set-status
    │
    ├─ Usage monitoring
    │    GET  /usage           → real-time stats via pull_usage_data
    │    GET  /usage-limits    → parsed usage_limits.json
    │    POST /usage-settings  → save usage_limits.json fields
    │
    └─ Profile & Smart Apply
         GET  /profile, POST /profile, /update-profile
         POST /smart-apply, /smart-apply-config

BACKGROUND THREADS
------------------
  auto-restart (30s poll)
    Checks run_state.json.  When status=rate_limited and resume_at has passed,
    and managed_by_loop is False and auto_restart is not False,
    re-spawns test_claude_call.py to resume the paused session.

  scheduler (60s poll)
    Checks schedules.json for entries whose next_run <= now.
    Launches batch_loop.py with the schedule's settings (max_batches,
    session_cost_pct, weekly_cost_pct, repeat).
    Advances next_run to the same hour the following day.

STATE FILES (shared with scripts)
----------------------------------
  run_state.json     — single-batch run status (status, pid, resume_at, error)
  loop_state.json    — batch-loop status (status, pid, batches_run, error)
  schedules.json     — scheduled loop entries
  jobs.json          — discovered and ranked job listings
  runs.json          — historical per-run records
  search_queue.json  — keyword/location search entries and their status

Usage: python scripts/serve.py [port]   (default port 8000)
"""
import json
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Schedule manager (in-process, concurrent, adaptive-sleep scheduler)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from search_scripts.schedule_manager import get_manager  # noqa: E402

STATE_PATH_REL      = "run_state.json"
LOOP_STATE_PATH_REL = "loop_state.json"
SCHEDULES_PATH_REL  = "schedules.json"

ROOT = Path(__file__).parent.parent

# ── dev flag ──────────────────────────────────────────────────────────────────
# Set True to run test_claude_call.py instead of run_batch.py when the
# "Run Batch" button is pressed. Flip to False for production.
TEST_MODE = True


def read_json(path, default=None):
    """Read and parse a JSON file at `path` relative to ROOT.
    Returns `default` (or {}) if the file is absent or unparseable.
    """
    p = ROOT / path
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else (default or {})


def write_json(path, data):
    """Serialize `data` to pretty-printed JSON and write it to `path` relative to ROOT."""
    (ROOT / path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── tag similarity helpers (mirrors tag_create.py) ────────────────────────────

def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def tokenize(s):
    return set(re.split(r"[-_\s]+", s.lower().strip()))


def similarity(a, b):
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_tag_match(tags, new_id, new_label, new_aliases):
    """Return (match_type, tag_id): 'exact', 'similar', or (None, None)."""
    new_terms = {new_id, slugify(new_label)} | {slugify(a) for a in new_aliases}
    for tid, tag in tags.items():
        existing_terms = {tid, slugify(tag.get("label", ""))} | {
            slugify(a) for a in tag.get("aliases", [])
        }
        if new_terms & existing_terms:
            return "exact", tid
    for tid, tag in tags.items():
        sim_id = similarity(new_id, tid)
        sim_label = similarity(new_label, tag.get("label", ""))
        if max(sim_id, sim_label) >= 0.5:
            return "similar", tid
    return None, None


def _auto_restart_loop(root: Path, state_rel: str, test_mode: bool):
    """
    Background thread: when run_state.json shows status=rate_limited and
    resume_at has passed, automatically re-spawn the batch script.
    Checks every 30 seconds so restarts are timely without hammering disk.
    """
    state_path = root / state_rel
    while True:
        time.sleep(30)
        try:
            if not state_path.exists():
                continue
            state = json.loads(state_path.read_text("utf-8"))
            if state.get("status") != "rate_limited":
                continue
            # Skip if batch_loop.py is managing its own cooldown, or auto-restart
            # was explicitly disabled (e.g. repeat=0 one-shot run hit a limit).
            if state.get("managed_by_loop") or state.get("auto_restart") is False:
                continue
            resume_str = state.get("resume_at")
            if not resume_str:
                continue
            resume_at = datetime.fromisoformat(resume_str)
            if resume_at.tzinfo is None:
                resume_at = resume_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < resume_at:
                continue
            # Mark as starting before spawning to prevent duplicate launches
            state["status"] = "starting"
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            script = "test_claude_call.py" if test_mode else "run_batch.py"
            subprocess.Popen(
                [sys.executable, str(root / "scripts" / script)],
                cwd=str(root),
                creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW) if sys.platform == "win32" else 0,
            )
        except Exception:
            pass


def _is_pid_alive_static(pid) -> bool:
    """Check whether a PID is alive (works outside Handler class)."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return str(pid) in result.stdout
        else:
            import os as _os
            _os.kill(int(pid), 0)
            return True
    except Exception:
        return False


def _compute_next_run(hour_utc: int) -> str:
    """Return the ISO UTC timestamp of the next occurrence of the given UTC hour.

    Always returns a strictly future time: if the hour has already passed today,
    the result is tomorrow at that hour.  Used to initialize and advance
    schedule entries' next_run field.

    Args:
      hour_utc — integer 0-23 representing the UTC hour to schedule
    Returns:
      ISO 8601 string, e.g. "2026-05-22T03:00:00+00:00"
    """
    from datetime import timedelta
    now  = datetime.now(timezone.utc)
    cand = now.replace(hour=int(hour_utc), minute=0, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand.isoformat(timespec="seconds")


def build_loop_config(queue_ids: list, settings: dict) -> dict:
    """
    Translate a list of search_queue.json entry IDs and UI settings into a
    complete config dict ready to pass to ScheduleManager.schedule_loop().

    Resume case: if settings contains remaining_loop_prompts, skip queue lookup
    and use those directly as nextLoop_prompt_dynamic.

    Reads workflow.md (or primary_prompt_file if specified) for primary_prompt.
    One entry in nextLoop_prompt_dynamic is produced per selected queue item.

    Args:
        queue_ids  — list of int ids from search_queue.json (may be empty for resume)
        settings   — dict from the POST body (thresholds, repeat, allow_reschedule, …)

    Returns a merged dict with all keys schedule_loop() expects.
    """
    # Resume case: remaining_loop_prompts already provided — skip queue lookup
    if settings.get("remaining_loop_prompts"):
        dynamic_prompts = settings["remaining_loop_prompts"]
    else:
        queue_data = read_json("search_queue.json", {"queue": []})
        by_id = {e["id"]: e for e in queue_data.get("queue", [])}
        dynamic_prompts = [
            f"keyword: {by_id[qid]['keyword']}, location: {by_id[qid]['location']}"
            for qid in queue_ids
            if qid in by_id
        ]

    # Resolve primary prompt from file picker selection or fall back to workflow.md
    primary_file = settings.get("primary_prompt_file", "workflow.md")
    primary_path = ROOT / primary_file
    if not primary_path.exists() or primary_path.suffix.lower() != ".md":
        primary_path = ROOT / "workflow.md"
    primary = (
        settings.get("primary_prompt")
        or (primary_path.read_text(encoding="utf-8") if primary_path.exists() else "")
    )
    static = settings.get("nextLoop_prompt_static") or "Continue the job search workflow."

    return {
        **settings,
        "primary_prompt":          primary,
        "nextLoop_prompt_static":  static,
        "nextLoop_prompt_dynamic": dynamic_prompts,
        # allowed_tools passes through from settings if the UI provides it;
        # None here means run_claude() will fall back to its ALLOWED_TOOLS constant.
        "allowed_tools":           settings.get("allowed_tools"),
    }


def _schedule_runner_loop(root: Path, schedules_rel: str, loop_state_rel: str):
    """
    Background thread: every 60 s check schedules.json for due entries and
    launch batch_loop.py when one is ready (only if no loop is already running).
    """
    loop_state_path = root / loop_state_rel
    schedules_path  = root / schedules_rel
    while True:
        time.sleep(60)
        try:
            # Skip if loop already running
            if loop_state_path.exists():
                ls = json.loads(loop_state_path.read_text("utf-8"))
                if ls.get("status") == "running":
                    pid = ls.get("pid")
                    if pid and _is_pid_alive_static(pid):
                        continue

            if not schedules_path.exists():
                continue
            data      = json.loads(schedules_path.read_text("utf-8-sig"))
            schedules = data.get("schedules", [])
            now_iso   = datetime.now(timezone.utc).isoformat(timespec="seconds")

            for sched in schedules:
                next_run = sched.get("next_run", "")
                if not next_run or next_run > now_iso:
                    continue

                # Build command
                cmd = [sys.executable, str(root / "scripts" / "batch_loop.py"),
                       "--schedule-id", sched["id"]]
                if sched.get("max_batches"):
                    cmd += ["--max", str(sched["max_batches"])]
                if sched.get("repeat") is not None:
                    cmd += ["--repeat", str(sched["repeat"])]
                if sched.get("session_cost_pct") is not None:
                    cmd += ["--session-cost-pct", str(sched["session_cost_pct"])]
                if sched.get("weekly_cost_pct") is not None:
                    cmd += ["--cost-pct", str(sched["weekly_cost_pct"])]
                # hour arg keeps loop in scheduled-window mode
                if sched.get("hour_utc") is not None:
                    cmd += ["--hour", str(sched["hour_utc"])]

                subprocess.Popen(
                    cmd, cwd=str(root),
                    creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW) if sys.platform == "win32" else 0,
                )

                # Advance next_run to the following day
                sched["last_run"] = now_iso
                sched["next_run"] = _compute_next_run(sched["hour_utc"])
                data["schedules"] = schedules
                schedules_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                break  # launch at most one per tick
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    """HTTP request handler for the Job Scout dashboard.

    Routes GET requests to _serve_* methods and POST requests to _post_* methods.
    All responses include CORS headers so the browser can call the API without
    cross-origin restrictions.  JSON bodies are parsed via _read_body(); all file
    writes go through write_json() to ensure consistent encoding and formatting.
    Handler instances are created per request and share no state between requests —
    all state is read from and written to JSON files in ROOT.
    """

    # ── routing ───────────────────────────────────────────────────────────────

    def do_GET(self):
        routes = {
            "/":               ("dashboard.html", "text/html; charset=utf-8"),
            "/dashboard.html": ("dashboard.html", "text/html; charset=utf-8"),
        }
        if self.path in routes:
            f, ct = routes[self.path]
            self._serve_file(ROOT / f, ct)
        elif self.path == "/jobs":
            self._serve_json("jobs.json", {"jobs": []})
        elif self.path == "/status":
            self._serve_json("status.json", {})
        elif self.path == "/runs":
            self._serve_json("runs.json", {"runs": []})
        elif self.path == "/tags":
            self._serve_json("tags.json", {"tags": {}, "categories": {}})
        elif self.path == "/pending-tags":
            self._serve_pending_tags()
        elif self.path == "/run-status":
            self._serve_run_status()
        elif self.path == "/usage":
            self._serve_usage()
        elif self.path == "/queue-preview":
            self._serve_queue_preview()
        elif self.path == "/queue-manage":
            self._serve_queue_manage()
        elif self.path == "/usage-limits":
            self._serve_usage_limits()
        elif self.path == "/smart-apply-config":
            self._serve_smart_apply_config()
        elif self.path.startswith("/run-log"):
            self._serve_run_log()
        elif self.path == "/profile":
            self._serve_file(ROOT / "profile.md", "text/plain; charset=utf-8")
        elif self.path == "/loop-status":
            self._serve_loop_status()
        elif self.path == "/schedules":
            self._serve_schedules()
        elif self.path == "/list-md-files":
            self._serve_list_md_files()
        elif self.path == "/loop-log":
            self._serve_loop_log()
        elif self.path.startswith("/tabs/"):
            rel = self.path.lstrip("/")
            ct = "application/javascript" if rel.endswith(".js") else "text/html; charset=utf-8"
            self._serve_file(ROOT / rel, ct)
        else:
            self._respond(404, b"Not Found")

    def do_POST(self):
        body = self._read_body()
        if self.path == "/status":
            self._post_status(body)
        elif self.path == "/tags/update":
            self._post_tags_update(body)
        elif self.path == "/tags/merge":
            self._post_tags_merge(body)
        elif self.path == "/tags/create":
            self._post_tags_create(body)
        elif self.path == "/tags/approve":
            self._post_tags_approve(body)
        elif self.path == "/tags/reject":
            self._post_tags_reject(body)
        elif self.path == "/run-dedup":
            self._post_run_dedup()
        elif self.path == "/run-batch":
            self._post_run_batch()
        elif self.path == "/run-stop":
            self._post_run_stop()
        elif self.path == "/queue-settings":
            self._post_queue_settings(body)
        elif self.path == "/queue-reorder":
            self._post_queue_reorder(body)
        elif self.path == "/queue-toggle-skip":
            self._post_queue_toggle_skip(body)
        elif self.path == "/queue-remove":
            self._post_queue_remove(body)
        elif self.path == "/queue-set-status":
            self._post_queue_set_status(body)
        elif self.path == "/usage-settings":
            self._post_usage_settings(body)
        elif self.path == "/smart-apply":
            self._post_smart_apply(body)
        elif self.path == "/smart-apply-config":
            self._post_smart_apply_config(body)
        elif self.path == "/open-editor":
            self._post_open_editor()
        elif self.path == "/update-profile":
            self._post_update_profile(body)
        elif self.path == "/profile":
            self._post_profile(body)
        elif self.path == "/run-loop":
            self._post_run_loop(body)
        elif self.path == "/stop-loop":
            self._post_stop_loop()
        elif self.path == "/schedule-loop":
            self._post_schedule_loop(body)
        elif self.path == "/cancel-schedule":
            self._post_cancel_schedule(body)
        elif self.path == "/run-schedule-now":
            self._post_run_schedule_now(body)
        elif self.path == "/continue-run":
            self._post_continue_run(body)
        elif self.path == "/dismiss-run":
            self._post_dismiss_run(body)
        elif self.path == "/dismiss-loop-error":
            self._post_dismiss_loop_error()
        elif self.path == "/return-remaining-to-queue":
            self._post_return_remaining_to_queue(body)
        else:
            self._respond(404, b"Not Found")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    # ── GET handlers ──────────────────────────────────────────────────────────

    def _serve_pending_tags(self):
        """Return only tags with status=pending from tags.json."""
        data = read_json("tags.json", {"tags": {}})
        pending = {tid: t for tid, t in data.get("tags", {}).items()
                   if t.get("status") == "pending"}
        body = json.dumps({"pending": list(pending.values())}, ensure_ascii=False).encode("utf-8")
        self._respond(200, body, "application/json")

    # ── POST handlers ─────────────────────────────────────────────────────────

    def _post_status(self, body):
        job_id = body.get("job_id")
        if not job_id:
            return self._respond(400, b'{"error":"missing job_id"}', "application/json")
        data = read_json("status.json", {})
        entry = {k: v for k, v in body.items() if k != "job_id"}
        entry.setdefault("updated", str(date.today()))
        data[job_id] = entry
        write_json("status.json", data)
        self._ok()

    def _post_tags_update(self, body):
        """Update one tag's fields (weight, description, label, aliases, category)."""
        tag_id = body.get("id")
        if not tag_id:
            return self._respond(400, b'{"error":"missing id"}', "application/json")
        tags_data = read_json("tags.json", {"tags": {}})
        if tag_id not in tags_data.get("tags", {}):
            return self._respond(404, b'{"error":"tag not found"}', "application/json")
        allowed = {"weight", "label", "description", "aliases", "category", "status"}
        for k, v in body.items():
            if k in allowed:
                tags_data["tags"][tag_id][k] = v
        write_json("tags.json", tags_data)
        self._ok()

    def _post_tags_create(self, body):
        """Create a new tag with status=pending after similarity check."""
        tag_id = body.get("id")
        label = body.get("label")
        if not tag_id or not label:
            return self._respond(400, b'{"error":"missing id or label"}', "application/json")
        required = {"id", "label", "weight", "category", "description"}
        missing = required - set(body.keys())
        if missing:
            err = json.dumps({"error": f"missing fields: {', '.join(missing)}"}).encode()
            return self._respond(400, err, "application/json")

        tags_data = read_json("tags.json", {"tags": {}})
        tags = tags_data.get("tags", {})
        aliases = body.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",") if a.strip()]

        match_type, matched_id = find_tag_match(tags, tag_id, label, aliases)
        if match_type == "exact":
            body_out = json.dumps({"status": "exists", "matched_id": matched_id}).encode()
            return self._respond(409, body_out, "application/json")
        if match_type == "similar":
            body_out = json.dumps({
                "status": "similar", "matched_id": matched_id,
                "suggestion": f"Consider using or aliasing '{matched_id}' instead"
            }).encode()
            return self._respond(409, body_out, "application/json")

        clean_id = re.sub(r"[^a-z0-9]+", "-", tag_id.lower()).strip("-")
        new_tag = {
            "id": clean_id,
            "label": label,
            "category": body["category"],
            "weight": int(body["weight"]),
            "status": "pending",
            "description": body["description"],
            "aliases": aliases,
        }
        if body.get("proposed_reason"):
            new_tag["proposed_reason"] = body["proposed_reason"]

        tags[clean_id] = new_tag
        tags_data["tags"] = tags
        write_json("tags.json", tags_data)
        self._respond(201, json.dumps({"status": "created", "id": clean_id}).encode(), "application/json")

    def _post_tags_approve(self, body):
        """Set a pending tag's status to approved."""
        tag_id = body.get("id")
        if not tag_id:
            return self._respond(400, b'{"error":"missing id"}', "application/json")
        tags_data = read_json("tags.json", {"tags": {}})
        tag = tags_data.get("tags", {}).get(tag_id)
        if not tag:
            return self._respond(404, b'{"error":"tag not found"}', "application/json")
        tag["status"] = "approved"
        tag.pop("proposed_reason", None)
        write_json("tags.json", tags_data)
        self._ok()

    def _post_tags_reject(self, body):
        """Remove a pending tag from tags.json entirely."""
        tag_id = body.get("id")
        if not tag_id:
            return self._respond(400, b'{"error":"missing id"}', "application/json")
        tags_data = read_json("tags.json", {"tags": {}})
        if tag_id not in tags_data.get("tags", {}):
            return self._respond(404, b'{"error":"tag not found"}', "application/json")
        del tags_data["tags"][tag_id]
        write_json("tags.json", tags_data)
        self._ok()

    def _post_tags_merge(self, body):
        """Merge source_id into target_id across all jobs."""
        src = body.get("source_id")
        tgt = body.get("target_id")
        if not src or not tgt:
            return self._respond(400, b'{"error":"missing source_id or target_id"}', "application/json")
        tags_data = read_json("tags.json", {"tags": {}})
        tags = tags_data["tags"]
        if src not in tags or tgt not in tags:
            return self._respond(404, b'{"error":"tag not found"}', "application/json")

        src_tag = tags[src]
        tgt_tag = tags[tgt]
        merged_aliases = list(set(
            tgt_tag.get("aliases", []) +
            src_tag.get("aliases", []) +
            [src, src_tag.get("label", "").lower().replace(" ", "-")]
        ))
        tgt_tag["aliases"] = merged_aliases
        del tags[src]
        write_json("tags.json", tags_data)

        jobs = read_json("jobs.json", {"jobs": []})
        for job in jobs["jobs"]:
            job_tags = job.get("tags", [])
            if src in job_tags:
                job_tags.remove(src)
                if tgt not in job_tags:
                    job_tags.append(tgt)
            job["tags"] = job_tags
        write_json("jobs.json", jobs)
        self._ok()

    # ── Batch run handlers ────────────────────────────────────────────────────

    def _is_pid_alive(self, pid):
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                return str(pid) in result.stdout
            else:
                import os
                os.kill(pid, 0)
                return True
        except Exception:
            return False

    def _serve_queue_preview(self):
        """Compute which searches would run in the next batch (mirrors workflow Step 1).
        Entries with skip_next=true are excluded from selection."""
        data = read_json("search_queue.json", {"settings": {}, "queue": []})
        settings = data.get("settings", {})
        batch_size = int(settings.get("batch_size", 10))
        rerun_days = int(settings.get("rerun_after_days", 14))
        queue = data.get("queue", [])
        today = date.today()

        pending = sorted(
            [e for e in queue if e.get("status") == "pending" and not e.get("skip_next")],
            key=lambda e: e.get("order", e.get("id", 0)),
        )
        due = []
        if len(pending) < batch_size:
            for e in sorted(queue, key=lambda e: e.get("id", 0)):
                if e.get("status") != "done" or e.get("skip_next"):
                    continue
                lr = e.get("last_run")
                if lr:
                    try:
                        delta = (today - date.fromisoformat(lr)).days
                    except ValueError:
                        delta = rerun_days + 1
                else:
                    delta = rerun_days + 1
                if delta > rerun_days:
                    due.append({**e, "_days_since": delta})

        # Count skipped entries for UI display
        skipped_count = sum(1 for e in queue if e.get("skip_next"))

        slots_for_due = max(0, batch_size - len(pending))
        selected_pending = pending[:batch_size]
        selected_due = due[:slots_for_due]

        body = json.dumps({
            "settings": {"batch_size": batch_size, "rerun_after_days": rerun_days},
            "total": len(queue),
            "pending_count": len(pending),
            "due_count": len(due),
            "skipped_count": skipped_count,
            "selected": selected_pending + selected_due,
            "selected_pending": selected_pending,
            "selected_due": selected_due,
        }, ensure_ascii=False).encode("utf-8")
        self._respond(200, body, "application/json")

    def _serve_usage_limits(self):
        """Return parsed usage_limits.json (non-comment keys only)."""
        try:
            raw = json.loads((ROOT / "usage_limits.json").read_text(encoding="utf-8"))
            clean = {k: v for k, v in raw.items() if not k.startswith("_")}
        except Exception:
            clean = {}
        self._respond(200, json.dumps(clean, ensure_ascii=False).encode(), "application/json")

    def _serve_queue_manage(self):
        """Return full queue sorted by id for the queue manager UI."""
        data = read_json("search_queue.json", {"settings": {}, "queue": []})
        queue = sorted(data.get("queue", []), key=lambda e: e.get("id", 0))
        settings = data.get("settings", {})
        rerun_days = int(settings.get("rerun_after_days", 14))
        today = date.today()
        # Annotate entries with days_since for the UI
        annotated = []
        for e in queue:
            entry = dict(e)
            lr = e.get("last_run")
            if lr:
                try:
                    entry["_days_since"] = (today - date.fromisoformat(lr)).days
                except ValueError:
                    entry["_days_since"] = None
            else:
                entry["_days_since"] = None
            entry["_due"] = (
                e.get("status") == "done" and
                entry["_days_since"] is not None and
                entry["_days_since"] > rerun_days
            )
            annotated.append(entry)
        body = json.dumps({
            "queue": annotated,
            "total": len(annotated),
            "settings": settings,
        }, ensure_ascii=False).encode("utf-8")
        self._respond(200, body, "application/json")

    def _serve_usage(self):
        """Return current Claude Code usage stats from ~/.claude/projects JSONL files."""
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from pull_usage_data import get_usage  # type: ignore
            data = get_usage(hours_back=168)

            def _serial(obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                raise TypeError(f"Not serializable: {type(obj)}")

            body = json.dumps(data, default=_serial, ensure_ascii=False).encode("utf-8")
            self._respond(200, body, "application/json")
        except Exception as exc:
            self._respond(500, json.dumps({"error": str(exc)}).encode(), "application/json")

    def _serve_run_status(self):
        """Return run_state.json, reconciling stale "running" state before responding.

        If status=running but the PID is no longer alive, sets status=error so the
        dashboard shows the failure immediately.  If the PID was never written (script
        crashed before Popen), allows a 10-second grace window before flagging error.
        Appends test_mode so the dashboard knows which batch script was used.

        Returns: run_state dict as JSON with an added test_mode boolean.
        """
        state = read_json(STATE_PATH_REL, {"status": "idle"})
        # Reconcile stale "running" state
        if state.get("status") == "running":
            pid = state.get("pid")
            if pid and not self._is_pid_alive(pid):
                state["status"] = "error"
                state["error"] = "process died unexpectedly"
                write_json(STATE_PATH_REL, state)
            elif not pid:
                # pid never set — batch script crashed before Popen; treat as error
                # Allow a 10s grace window for very fresh starts
                try:
                    from datetime import datetime as _dt, timezone
                    started = state.get("started", "")
                    age = (_dt.now() - _dt.fromisoformat(started)).total_seconds() if started else 999
                except Exception:
                    age = 999
                if age > 10:
                    state["status"] = "error"
                    state["error"] = "batch script exited before starting Claude (check PATH / claude CLI install)"
                    write_json(STATE_PATH_REL, state)
        state["test_mode"] = TEST_MODE
        body = json.dumps(state, ensure_ascii=False).encode("utf-8")
        self._respond(200, body, "application/json")

    def _serve_run_log(self):
        max_lines = 500
        if "?lines=" in self.path:
            try:
                max_lines = int(self.path.split("?lines=")[1].split("&")[0])
            except ValueError:
                pass
        state = read_json(STATE_PATH_REL, {})
        log_name = state.get("log")
        if not log_name:
            return self._respond(200, b"No log available", "text/plain")
        log_path = ROOT / "logs" / log_name
        if not log_path.exists():
            return self._respond(200, b"Log file not found", "text/plain")
        try:
            all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(all_lines[-max_lines:])
            self._respond(200, tail.encode("utf-8"), "text/plain; charset=utf-8")
        except Exception as e:
            self._respond(500, str(e).encode(), "text/plain")

    def _post_run_batch(self):
        """Launch test_claude_call.py as a detached subprocess (single-run mode).

        Returns 409 if a batch is already running (PID still alive in process list).
        Returns 202 on successful Popen — the process is starting, not yet confirmed running.
        Uses DETACHED_PROCESS | CREATE_NO_WINDOW on Windows so no console window appears.

        Input: none (no request body needed)
        Output: {"status": "started"} on 202, or {"error": "..."} on 409/500
        """
        state = read_json(STATE_PATH_REL, {"status": "idle"})
        if state.get("status") == "running":
            pid = state.get("pid")
            if pid and self._is_pid_alive(pid):
                body = json.dumps({"error": "batch already running", "pid": pid}).encode()
                return self._respond(409, body, "application/json")
        script = "test_claude_call.py" if TEST_MODE else "run_batch.py"
        try:
            subprocess.Popen(
                [sys.executable, str(ROOT / "scripts" / script)],
                cwd=str(ROOT),
                creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW) if sys.platform == "win32" else 0,
            )
            self._respond(202, b'{"status":"started"}', "application/json")
        except Exception as e:
            self._respond(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _post_queue_reorder(self, body):
        """Accept a new ordered list of entry ids and update each entry's 'order' field."""
        order_list = body.get("order")
        if not isinstance(order_list, list):
            return self._respond(400, b'{"error":"order must be a list of ids"}', "application/json")
        data = read_json("search_queue.json", {"settings": {}, "queue": []})
        order_map = {eid: idx for idx, eid in enumerate(order_list)}
        for entry in data.get("queue", []):
            eid = entry.get("id")
            if eid in order_map:
                entry["order"] = order_map[eid]
        write_json("search_queue.json", data)
        self._ok()

    def _post_queue_toggle_skip(self, body):
        """Toggle skip_next on a queue entry. Skipped entries are excluded from the next batch."""
        entry_id = body.get("id")
        if entry_id is None:
            return self._respond(400, b'{"error":"missing id"}', "application/json")
        data = read_json("search_queue.json", {"settings": {}, "queue": []})
        for entry in data.get("queue", []):
            if entry.get("id") == entry_id:
                entry["skip_next"] = not entry.get("skip_next", False)
                write_json("search_queue.json", data)
                out = json.dumps({"ok": True, "skip_next": entry["skip_next"]}).encode()
                return self._respond(200, out, "application/json")
        self._respond(404, b'{"error":"entry not found"}', "application/json")

    def _post_queue_remove(self, body):
        """Permanently remove a queue entry by id."""
        entry_id = body.get("id")
        if entry_id is None:
            return self._respond(400, b'{"error":"missing id"}', "application/json")
        data = read_json("search_queue.json", {"settings": {}, "queue": []})
        before = len(data.get("queue", []))
        data["queue"] = [e for e in data.get("queue", []) if e.get("id") != entry_id]
        if len(data["queue"]) == before:
            return self._respond(404, b'{"error":"entry not found"}', "application/json")
        write_json("search_queue.json", data)
        self._ok()

    def _post_queue_set_status(self, body):
        """Set status on a queue entry (e.g. pending to force it into the next batch)."""
        entry_id = body.get("id")
        new_status = body.get("status")
        if entry_id is None or new_status not in ("pending", "done"):
            return self._respond(400, b'{"error":"missing id or invalid status"}', "application/json")
        data = read_json("search_queue.json", {"settings": {}, "queue": []})
        for entry in data.get("queue", []):
            if entry.get("id") == entry_id:
                entry["status"] = new_status
                if new_status == "pending":
                    entry.pop("last_run", None)
                    entry.pop("skip_next", None)
                write_json("search_queue.json", data)
                return self._ok()
        self._respond(404, b'{"error":"entry not found"}', "application/json")

    def _post_queue_settings(self, body):
        """Update batch_size and/or rerun_after_days in search_queue.json."""
        data = read_json("search_queue.json", {"settings": {}, "queue": []})
        settings = data.setdefault("settings", {})
        changed = False
        for key in ("batch_size", "rerun_after_days"):
            if key in body:
                val = int(body[key])
                if key == "batch_size":
                    val = max(1, min(val, 50))
                elif key == "rerun_after_days":
                    val = max(1, min(val, 365))
                settings[key] = val
                changed = True
        if not changed:
            return self._respond(400, b'{"error":"no valid fields"}', "application/json")
        write_json("search_queue.json", data)
        self._respond(200, json.dumps({"ok": True, "settings": settings}).encode(), "application/json")

    def _post_run_stop(self):
        """Kill the running batch process and mark state as aborted."""
        state = read_json(STATE_PATH_REL, {"status": "idle"})
        pid = state.get("pid")
        killed = False
        if pid:
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    import os as _os
                    _os.kill(int(pid), 9)
                killed = True
            except Exception:
                pass
        from datetime import datetime as _dt
        state["status"] = "error"
        state["error"] = "aborted by user"
        state["ended"] = _dt.now().isoformat(timespec="seconds")
        state["pid"] = None
        write_json(STATE_PATH_REL, state)
        self._respond(200, json.dumps({"ok": True, "killed": killed}).encode(), "application/json")

    def _serve_smart_apply_config(self):
        self._serve_json("smart_apply_config.json",
                         {"default_parent_folder": "", "default_resume_path": "",
                          "default_cover_letter_path": "", "applications": []})

    def _post_smart_apply_config(self, body):
        """Persist default paths for Smart Apply."""
        cfg = read_json("smart_apply_config.json",
                        {"default_parent_folder": "", "default_resume_path": "",
                         "default_cover_letter_path": "", "applications": []})
        for key in ("default_parent_folder", "default_resume_path", "default_cover_letter_path"):
            if key in body:
                cfg[key] = body[key]
        write_json("smart_apply_config.json", cfg)
        self._ok()

    def _post_smart_apply(self, body):
        """
        Create an application folder for a job.
        body: { job_id, folder_name, parent_folder, resume_path, cover_letter_path }
        Steps:
          1. Create folder <parent_folder>/<folder_name>/
          2. Write JD.txt with job description
          3. Copy resume and cover letter into folder
          4. Open VS Code in the new folder
          5. Record in smart_apply_config.json applications list
        """
        import shutil as _shutil
        import os as _os

        job_id      = body.get("job_id")
        folder_name = body.get("folder_name", "").strip()
        parent      = body.get("parent_folder", "").strip()
        resume_src  = body.get("resume_path", "").strip()
        cl_src      = body.get("cover_letter_path", "").strip()

        if not folder_name or not parent:
            return self._respond(400, b'{"error":"missing folder_name or parent_folder"}', "application/json")

        # Sanitize folder name (no path separators)
        folder_name = re.sub(r'[<>:"/\\|?*]', "-", folder_name)
        target = Path(parent) / folder_name

        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return self._respond(500, json.dumps({"error": f"Could not create folder: {e}"}).encode(), "application/json")

        errors = []

        # Write JD.txt
        if job_id:
            jobs = read_json("jobs.json", {"jobs": []})
            job = next((j for j in jobs.get("jobs", []) if j.get("indeed_id") == job_id), None)
            if job:
                jd_text = (
                    f"Job: {job.get('title', '')}\n"
                    f"Company: {job.get('company', '')}\n"
                    f"Location: {job.get('location', '')}\n"
                    f"Salary: {job.get('salary', 'Not listed')}\n"
                    f"URL: {job.get('url', '')}\n"
                    f"Posted: {job.get('posted', '')}\n\n"
                    f"--- Job Description ---\n{job.get('summary', '(No description cached)')}"
                )
                try:
                    (target / "JD.txt").write_text(jd_text, encoding="utf-8")
                except Exception as e:
                    errors.append(f"JD.txt: {e}")

        # Copy resume
        if resume_src and Path(resume_src).exists():
            try:
                _shutil.copy2(resume_src, target / Path(resume_src).name)
            except Exception as e:
                errors.append(f"resume copy: {e}")
        elif resume_src:
            errors.append(f"resume not found: {resume_src}")

        # Copy cover letter
        if cl_src and Path(cl_src).exists():
            try:
                _shutil.copy2(cl_src, target / Path(cl_src).name)
            except Exception as e:
                errors.append(f"cover letter copy: {e}")
        elif cl_src:
            errors.append(f"cover letter not found: {cl_src}")

        # Open VS Code in the new folder
        try:
            folder_str = str(target)
            if sys.platform == "win32":
                subprocess.Popen(f'code "{folder_str}"', shell=True, cwd=folder_str)
            else:
                subprocess.Popen(["code", folder_str], cwd=folder_str)
        except Exception as e:
            errors.append(f"VS Code open: {e}")

        # Record in config
        cfg = read_json("smart_apply_config.json",
                        {"default_parent_folder": "", "default_resume_path": "",
                         "default_cover_letter_path": "", "applications": []})
        apps = cfg.setdefault("applications", [])
        if job_id:
            jobs = read_json("jobs.json", {"jobs": []})
            job_obj = next((j for j in jobs.get("jobs", []) if j.get("indeed_id") == job_id), {})
        else:
            job_obj = {}
        apps.insert(0, {
            "job_id":    job_id,
            "company":   job_obj.get("company", ""),
            "title":     job_obj.get("title", ""),
            "folder":    str(target),
            "created":   str(date.today()),
        })
        write_json("smart_apply_config.json", cfg)

        out = json.dumps({
            "ok": True, "folder": str(target),
            "errors": errors if errors else None,
        }, ensure_ascii=False).encode()
        self._respond(200, out, "application/json")

    def _post_usage_settings(self, body):
        """Save usage_limits.json fields from the UI."""
        allowed = {
            "plan", "session_cost_usd", "weekly_cost_usd",
            "check_interval_seconds", "reopen_delay_hours", "warn_at_pct",
            "loop_max_batches", "loop_session_cost_pct", "loop_weekly_cost_pct",
            "loop_schedule_hour_utc", "loop_repeat",
        }
        # Read current file preserving comment keys
        try:
            raw = json.loads((ROOT / "usage_limits.json").read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        changed = False
        for k, v in body.items():
            if k not in allowed:
                continue
            if v == "" or v is None:
                raw[k] = None
            else:
                raw[k] = v
            changed = True
        if not changed:
            return self._respond(400, b'{"error":"no valid fields"}', "application/json")
        (ROOT / "usage_limits.json").write_text(
            json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._respond(200, json.dumps({"ok": True}).encode(), "application/json")

    def _post_open_editor(self):
        """Launch VS Code in the project root."""
        try:
            if sys.platform == "win32":
                # shell=True required on Windows so cmd.exe resolves code.cmd
                subprocess.Popen(f'code "{ROOT}"', shell=True, cwd=str(ROOT))
            else:
                subprocess.Popen(["code", str(ROOT)], cwd=str(ROOT))
            self._ok()
        except Exception as e:
            self._respond(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _post_update_profile(self, body):
        """Write document text to temp_document.txt then spawn Claude to update profile.md."""
        text = body.get("text", "").strip()
        if not text:
            return self._respond(400, b'{"error":"missing text"}', "application/json")
        (ROOT / "temp_document.txt").write_text(text, encoding="utf-8")
        prompt_path = ROOT / "scripts" / "update_profile_prompt.md"
        if not prompt_path.exists():
            return self._respond(404, b'{"error":"update_profile_prompt.md not found"}', "application/json")
        try:
            subprocess.Popen(
                ["claude", "-p", str(prompt_path),
                 "--allowedTools", "Read,Write,Edit,TodoWrite"],
                cwd=str(ROOT),
                creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW) if sys.platform == "win32" else 0,
            )
            self._respond(202, b'{"status":"started"}', "application/json")
        except FileNotFoundError:
            self._respond(500, b'{"error":"claude CLI not found"}', "application/json")

    def _post_profile(self, body):
        """Save profile.md content directly."""
        text = body.get("content", "")
        if not text:
            return self._respond(400, b'{"error":"missing content"}', "application/json")
        (ROOT / "profile.md").write_text(text, encoding="utf-8")
        self._ok()

    def _post_run_dedup(self):
        """Spawn a Claude dedup run in the background."""
        prompt_path = ROOT / "scripts" / "dedup_tags_prompt.md"
        if not prompt_path.exists():
            return self._respond(404, b'{"error":"dedup_tags_prompt.md not found"}', "application/json")
        try:
            subprocess.Popen(
                ["claude", "-p", str(prompt_path),
                 "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,TodoWrite"],
                cwd=str(ROOT),
                creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW) if sys.platform == "win32" else 0,
            )
            self._respond(202, b'{"status":"started"}', "application/json")
        except FileNotFoundError:
            self._respond(500, b'{"error":"claude CLI not found"}', "application/json")

    # ── Loop / Schedule handlers ──────────────────────────────────────────────

    def _serve_loop_status(self):
        """Return loop_state.json, reconciling stale running state via in-process manager."""
        state = read_json(LOOP_STATE_PATH_REL, {"status": "idle"})
        if state.get("status") == "running":
            mgr = get_manager()
            active_sched_id = state.get("active_sched_id")
            schedules = mgr.get_schedules()
            sched = next((s for s in schedules if s.get("id") == active_sched_id), None) \
                    if active_sched_id else None
            is_really_running = bool(sched and sched.get("_is_running"))
            if not is_really_running:
                if sched and sched.get("status") not in ("active", "running"):
                    state["status"] = sched["status"]
                else:
                    state["status"] = "error"
                    state["error"]  = "loop process stopped unexpectedly"
                write_json(LOOP_STATE_PATH_REL, state)
        self._respond(200, json.dumps(state, ensure_ascii=False).encode(), "application/json")

    def _serve_schedules(self):
        entries = get_manager().get_schedules()
        # If a schedule has status "running" but _is_running is False (manager not
        # executing it), mirror the correct terminal status from loop_state.json so
        # the dashboard card stops showing "RUNNING" after the loop ends.
        loop_state = read_json(LOOP_STATE_PATH_REL, {})
        for e in entries:
            if e.get("status") == "running" and not e.get("_is_running"):
                ls = loop_state.get("status", "error")
                e["status"] = ls if ls not in ("running", "idle") else "error"
        body = json.dumps({"schedules": entries}, ensure_ascii=False).encode("utf-8")
        self._respond(200, body, "application/json")

    def _post_run_loop(self, body):
        """Launch batch_loop.py immediately (alias for /schedule-loop with now=true)."""
        body["now"] = True
        self._post_schedule_loop(body)

    def _post_stop_loop(self):
        """
        Cancel the currently running schedule. Cancellation is graceful: the active
        run_loop() call runs to completion, then its result is discarded and the
        schedule entry is removed. The dashboard will see status → "idle" once done.
        """
        state    = read_json(LOOP_STATE_PATH_REL, {"status": "idle"})
        sched_id = state.get("active_sched_id") or state.get("id")
        cancelled = False
        if sched_id:
            cancelled = get_manager().cancel_schedule(sched_id)
        self._respond(200,
            json.dumps({"ok": True, "cancelled": cancelled, "sched_id": sched_id}).encode(),
            "application/json")

    def _post_schedule_loop(self, body):
        """
        Unified loop launcher (primary UI entry point).

        Expected body keys:
            queue_ids         list[int]  — search_queue.json entry IDs to run
            now               bool       — fire immediately (default False)
            hour_utc          int        — UTC hour for timed schedule (required if now=False)
            minute_utc        int        — UTC minute (default 0)
            repeat            int        — 0=once, -1=forever, N=N additional runs
            allow_reschedule  bool       — auto-reschedule on session/API limit
            session_threshold float      — default 80.0
            weekly_threshold  float      — default 80.0
            context_threshold float      — default 90.0

        Returns 202 with {"ok": true, "sched_id": "<uuid>"}.
        """
        run_now  = body.get("now", False)
        hour_utc = body.get("hour_utc") if body.get("hour_utc") is not None else body.get("schedule_hour_utc")

        if not run_now and hour_utc is None:
            return self._respond(400,
                b'{"error":"provide now=true and/or hour_utc 0-23"}', "application/json")
        if hour_utc is not None and not (0 <= int(hour_utc) <= 23):
            return self._respond(400, b'{"error":"hour_utc must be 0-23"}', "application/json")

        queue_ids = body.get("queue_ids") or []
        # Allow empty queue_ids when resuming from remaining_loop_prompts
        if not queue_ids and not body.get("remaining_loop_prompts"):
            return self._respond(400,
                b'{"error":"queue_ids required (or remaining_loop_prompts for resume)"}',
                "application/json")

        try:
            config = build_loop_config(queue_ids, body)
        except Exception as exc:
            return self._respond(500, json.dumps({"error": str(exc)}).encode(), "application/json")

        try:
            sched_id = get_manager().schedule_loop(config)
        except Exception as exc:
            return self._respond(500, json.dumps({"error": str(exc)}).encode(), "application/json")

        self._respond(202,
            json.dumps({"ok": True, "sched_id": sched_id}).encode(), "application/json")

    def _post_cancel_schedule(self, body):
        """Remove a schedule entry by id. If currently running, cancellation is deferred
        until the active run_loop() call returns (result is discarded)."""
        sched_id = body.get("id")
        if not sched_id:
            return self._respond(400, b'{"error":"id required"}', "application/json")
        found = get_manager().cancel_schedule(sched_id)
        if not found:
            return self._respond(404, b'{"error":"schedule not found"}', "application/json")
        self._respond(200, json.dumps({"ok": True, "sched_id": sched_id}).encode(), "application/json")

    def _post_run_schedule_now(self, body):
        """Fire an existing schedule immediately, ignoring its next_run time."""
        sched_id = body.get("id")
        if not sched_id:
            return self._respond(400, b'{"error":"id required"}', "application/json")
        fired = get_manager().run_schedule_now(sched_id)
        if fired is False:
            # False means already running; None/404 means not found
            return self._respond(409,
                json.dumps({"error": "schedule not found or already running"}).encode(),
                "application/json")
        self._respond(202,
            json.dumps({"ok": True, "sched_id": sched_id}).encode(), "application/json")

    def _post_dismiss_run(self, body):
        """Mark a run record as dismissed by its 'started' timestamp."""
        started = body.get("started")
        if not started:
            return self._respond(400, b'{"error":"started is required"}', "application/json")
        runs_path = ROOT / "runs.json"
        try:
            data = json.loads(runs_path.read_text("utf-8-sig")) if runs_path.exists() else {"runs": []}
            for r in data.get("runs", []):
                if r.get("started") == started:
                    r["dismissed"] = True
                    break
            runs_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            self._ok()
        except Exception as e:
            self._respond(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _post_dismiss_loop_error(self):
        """Clear an error/paused state from loop_state.json so the banner hides."""
        try:
            data = read_json(LOOP_STATE_PATH_REL, {})
            if data.get("status") in ("error", "paused", "stopped"):
                data["status"] = "idle"
                data["error"]  = None
                write_json(LOOP_STATE_PATH_REL, data)
            self._ok()
        except Exception as e:
            self._respond(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _post_continue_run(self, body):
        """
        Write test_session.json as a paused session with the given session_id and
        paused_entry, then launch test_claude_call.py.  The script will detect the
        paused state and resume with a bare "continue" prompt.
        """
        session_id   = body.get("session_id")
        paused_entry = body.get("entry")
        if not session_id or not paused_entry:
            return self._respond(400,
                b'{"error":"session_id and entry are required"}', "application/json")

        state = read_json(STATE_PATH_REL, {"status": "idle"})
        if state.get("status") == "running":
            pid = state.get("pid")
            if pid and self._is_pid_alive(pid):
                return self._respond(409,
                    json.dumps({"error": "batch already running", "pid": pid}).encode(),
                    "application/json")

        session_file = ROOT / "test_session.json"
        try:
            session_file.write_text(json.dumps({
                "session_id":   session_id,
                "last_used":    datetime.now().isoformat(timespec="seconds"),
                "valid":        True,
                "paused":       True,
                "paused_entry": paused_entry,
            }, indent=2), encoding="utf-8")
        except Exception as e:
            return self._respond(500, json.dumps({"error": str(e)}).encode(), "application/json")

        try:
            subprocess.Popen(
                [sys.executable, str(ROOT / "scripts" / "test_claude_call.py")],
                cwd=str(ROOT),
                creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW) if sys.platform == "win32" else 0,
            )
            self._respond(202, b'{"status":"started"}', "application/json")
        except Exception as e:
            self._respond(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _serve_list_md_files(self):
        """Return sorted list of .md filenames in the project root."""
        files = sorted(p.name for p in ROOT.glob("*.md") if p.is_file())
        self._respond(200, json.dumps({"files": files}).encode(), "application/json")

    def _serve_loop_log(self):
        """Return last 200 lines of the most recently modified loop_log_*.txt file."""
        logs_dir = ROOT / "logs"
        log_files = sorted(logs_dir.glob("loop_log_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not log_files:
            self._respond(200, json.dumps({"lines": []}).encode(), "application/json")
            return
        try:
            text  = log_files[0].read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()[-200:]
            self._respond(200, json.dumps({"lines": lines, "file": log_files[0].name}).encode(), "application/json")
        except Exception as exc:
            self._respond(500, json.dumps({"error": str(exc)}).encode(), "application/json")

    def _post_return_remaining_to_queue(self, body):
        """
        Given a limit_reached schedule ID, parse its remaining_loop_prompts
        back to queue entries (by keyword+location), reset them to pending,
        and mark the schedule as completed.
        """
        sched_id = body.get("id")
        if not sched_id:
            return self._respond(400, b'{"error":"id required"}', "application/json")

        try:
            schedules_path = ROOT / "schedules.json"
            if not schedules_path.exists():
                return self._respond(404, b'{"error":"schedules.json not found"}', "application/json")
            data = json.loads(schedules_path.read_text(encoding="utf-8"))
            entry = next((s for s in data.get("schedules", []) if s["id"] == sched_id), None)
            if not entry:
                return self._respond(404, b'{"error":"schedule not found"}', "application/json")

            remaining = entry.get("remaining_loop_prompts", [])
            queue_data = read_json("search_queue.json", {"queue": []})
            reset_count = 0
            for prompt in remaining:
                # Parse "keyword: X, location: Y"
                parts = {}
                for segment in prompt.split(","):
                    if ":" in segment:
                        k, v = segment.split(":", 1)
                        parts[k.strip().lower()] = v.strip().lower()
                kw  = parts.get("keyword", "")
                loc = parts.get("location", "")
                for q_entry in queue_data["queue"]:
                    if (q_entry.get("keyword", "").lower() == kw and
                            q_entry.get("location", "").lower() == loc):
                        q_entry["status"]    = "pending"
                        q_entry["skip_next"] = False
                        reset_count += 1
                        break

            write_json("search_queue.json", queue_data)
            entry["status"] = "completed"
            schedules_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            self._respond(200, json.dumps({"ok": True, "reset": reset_count}).encode(), "application/json")
        except Exception as exc:
            self._respond(500, json.dumps({"error": str(exc)}).encode(), "application/json")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n)) if n else {}
        except Exception:
            return {}

    def _serve_file(self, path, ct):
        if not path.exists():
            return self._respond(404, b"File not found")
        self._respond(200, path.read_bytes(), ct)

    def _serve_json(self, rel_path, default=None):
        p = ROOT / rel_path
        body = p.read_bytes() if p.exists() else json.dumps(default or {}).encode()
        self._respond(200, body, "application/json")

    def _ok(self):
        self._respond(200, b'{"ok":true}', "application/json")

    def _respond(self, code, body=b"", ct="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"Job Scout dashboard -> http://localhost:{port}")
    print("Ctrl+C to stop")
    sys.stdout.flush()
    import os as _os
    _devnull = open(_os.devnull, "w", encoding="utf-8")
    sys.stdout = _devnull
    sys.stderr = _devnull

    # Single-batch auto-restart (unchanged)
    threading.Thread(
        target=_auto_restart_loop,
        args=(ROOT, STATE_PATH_REL, TEST_MODE),
        daemon=True,
        name="auto-restart",
    ).start()

    # Schedule manager: in-process adaptive-sleep scheduler + thread pool
    # get_manager() starts the background tick thread on first call.
    get_manager()

    try:
        HTTPServer(("localhost", port), Handler).serve_forever()
    except KeyboardInterrupt:
        get_manager().shutdown()
