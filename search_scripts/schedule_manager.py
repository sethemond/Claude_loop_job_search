"""
schedule_manager.py — Concurrent multi-schedule orchestrator for run_loop.

Manages one or more timed (or immediate) loop_manager.run_loop() calls, each
running in its own thread-pool worker so schedules never block each other.

Primary entry point from the UI / serve.py:

    from search_scripts.schedule_manager import get_manager
    manager = get_manager()                      # starts background thread on first call
    sched_id = manager.schedule_loop(config)     # returns UUID immediately

Schedules are persisted in schedules.json. The background tick thread wakes at
the exact second of each scheduled run using adaptive sleep via threading.Event
(same pattern as CPython's sched module and APScheduler's BackgroundScheduler),
rather than polling on a fixed interval. Any mutation of schedules.json calls
_wakeup.set() to interrupt the current sleep immediately.

─── SCHEDULE ENTRY SCHEMA (schedules.json) ───────────────────────────────────

{
  "id":                          str       UUID4
  "status":                      str       "active" | "running" | "limit_reached"
                                           | "completed" | "error"
  "created":                     str       ISO UTC timestamp
  "hour_utc":                    int       UTC hour for recurring fires
  "minute_utc":                  int       UTC minute for recurring fires (default 0)
  "repeat":                      int       0=once, -1=forever, N=N additional runs
  "runs_remaining":              int       countdown; -1=forever
  "next_run":                    str       ISO UTC timestamp of next fire
  "last_run":                    str|null  ISO UTC timestamp of last fire
  "session_id":                  str|null  Claude session UUID for resumption
  "remaining_loop_prompts":      list[str] unfinished dynamic prompts from last run
  "continuing_from_limit_reached": bool   True when resuming after a limit interrupt
  "allow_reschedule":            bool      user choice: auto-reschedule on session limit
  "settings": {
    "primary_prompt":            str
    "nextLoop_prompt_static":    str
    "nextLoop_prompt_dynamic":   list[str]  one entry per search; len = batch size
    "session_threshold":         float
    "weekly_threshold":          float
    "context_threshold":         float
  }
  "last_result":                 dict|null  {success, limit_exceeded} from last run
}

─── REPEAT / runs_remaining SEMANTICS ────────────────────────────────────────

runs_remaining represents "how many more runs to execute after the current one."

  repeat=0  → runs_remaining=0  → run once; 0 more after → completed after 1st run
  repeat=3  → runs_remaining=3  → run 4 times total (first + 3 additional)
  repeat=-1 → runs_remaining=-1 → run forever

─── AUTO-RESCHEDULE vs SURFACE-TO-UI ─────────────────────────────────────────

When run_loop hits a limit:
  allow_reschedule=True  + reschedule_time set → next_run = reschedule_time; auto-retry
  allow_reschedule=False or no reschedule_time → status="limit_reached";
                                                  remaining_loop_prompts surfaced via
                                                  get_schedules() for dashboard to show
                                                  a "Resume" option.

Weekly-limit hits (limit_exceeded=2) never provide reschedule_time regardless of
allow_reschedule, so they always result in status="limit_reached".
"""

import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from search_scripts.loop_manager import run_loop  # noqa: E402 (after sys.path fixup)
from search_scripts.claude_call import terminate_active  # noqa: E402

_SCHEDULES_PATH  = _ROOT / "schedules.json"
_LOOP_STATE_PATH = _ROOT / "loop_state.json"
_RUNS_PATH       = _ROOT / "runs.json"

_MAX_WORKERS  = 8     # max concurrent run_loop calls
_TICK_CAP     = 60.0  # max sleep seconds even when no schedules are pending


# ── datetime helpers ──────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _next_occurrence(hour: int, minute: int, pattern: str, weekday_target: int, now: datetime) -> datetime:
    """Return the next future datetime matching hour:minute UTC under the given pattern.

    pattern: "daily" | "weekdays" | "weekends" | "weekly"
    weekday_target: 0=Mon … 6=Sun (only used when pattern="weekly")
    """
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)

    if pattern == "daily":
        return candidate

    for _ in range(7):
        wd = candidate.weekday()  # 0=Mon … 6=Sun
        if pattern == "weekdays" and wd < 5:
            return candidate
        if pattern == "weekends" and wd >= 5:
            return candidate
        if pattern == "weekly" and wd == weekday_target:
            return candidate
        candidate += timedelta(days=1)

    return candidate  # fallback (shouldn't happen with valid patterns)


# ── file I/O helpers ──────────────────────────────────────────────────────────

def _load_schedules() -> dict:
    if _SCHEDULES_PATH.exists():
        try:
            with open(_SCHEDULES_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "schedules" in data:
                return data
        except Exception:
            pass
    return {"schedules": []}


def _save_schedules(data: dict) -> None:
    with open(_SCHEDULES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _read_last_session_id() -> Optional[str]:
    """
    Return the session_id from the most recent record in runs.json.

    run_loop() does not return session_id in its result dict, but it appends
    every Claude call as a record to runs.json including the session_id used.
    Since run_loop() is synchronous, all records are written before it returns.
    """
    try:
        if not _RUNS_PATH.exists():
            return None
        with open(_RUNS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        runs = data.get("runs", []) if isinstance(data, dict) else data
        if isinstance(runs, list) and runs:
            return runs[-1].get("session_id")
    except Exception:
        pass
    return None


def _write_loop_state(
    status: str,
    sched_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    state: dict = {}
    try:
        if _LOOP_STATE_PATH.exists():
            with open(_LOOP_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
    except Exception:
        pass
    state["status"] = status
    if status == "running":
        state["pid"] = os.getpid()
    state["updated"] = _iso(_utcnow())
    if sched_id is not None:
        state["active_sched_id"] = sched_id
    if error is not None:
        state["error"] = error
    try:
        with open(_LOOP_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


# ── core class ────────────────────────────────────────────────────────────────

class ScheduleManager:
    """
    Singleton class managing multiple concurrent scheduled run_loop() calls.

    One daemon thread (tick thread) wakes at the exact second of each
    scheduled run using adaptive sleep + threading.Event. Each run_loop()
    call executes in a ThreadPoolExecutor worker so schedules never block
    each other.
    """

    def __init__(self) -> None:
        self._lock     = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=_MAX_WORKERS, thread_name_prefix="ScheduleWorker"
        )
        self._running:   dict[str, object] = {}  # sched_id → Future (or None placeholder)
        self._cancelled: set[str]          = set()
        self._wakeup   = threading.Event()
        self._shutdown = threading.Event()
        self._tick_thread = threading.Thread(
            target=self._tick_loop, daemon=True, name="ScheduleManagerTick"
        )

    def start(self) -> None:
        self._reconcile_orphans()
        self._tick_thread.start()

    def _reconcile_orphans(self) -> None:
        """Mark schedules stuck at 'running' from a prior server session as error."""
        with self._lock:
            data = _load_schedules()
            changed = False
            for e in data["schedules"]:
                if e.get("status") == "running":
                    e["status"] = "error"
                    if not e.get("last_result"):
                        e["last_result"] = {"success": 0, "limit_exceeded": 0}
                    changed = True
            if changed:
                _save_schedules(data)
        try:
            if _LOOP_STATE_PATH.exists():
                state = json.loads(_LOOP_STATE_PATH.read_text("utf-8"))
                if state.get("status") == "running":
                    state["status"] = "error"
                    state["error"] = "orphaned from previous server session"
                    state["updated"] = _iso(_utcnow())
                    _LOOP_STATE_PATH.write_text(json.dumps(state, indent=2), "utf-8")
        except Exception:
            pass

    def shutdown(self) -> None:
        self._shutdown.set()
        self._wakeup.set()
        self._tick_thread.join(timeout=5)
        self._executor.shutdown(wait=False)

    # ── background tick loop ──────────────────────────────────────────────────

    def _tick_loop(self) -> None:
        while not self._shutdown.is_set():
            self._wakeup.clear()
            sleep_secs = self._fire_due()
            self._wakeup.wait(timeout=sleep_secs)

    def _fire_due(self) -> float:
        """
        Fire all active schedules whose next_run <= now. Return seconds until
        the next scheduled fire (capped at _TICK_CAP so the thread stays
        responsive even when no schedules are pending).
        """
        now = _utcnow()
        to_fire: list[str]         = []
        next_times: list[datetime] = []

        with self._lock:
            data = _load_schedules()
            for entry in data["schedules"]:
                if entry.get("status") != "active":
                    continue
                sched_id = entry["id"]
                nr_str = entry.get("next_run")
                if not nr_str:
                    continue
                next_run = _parse_iso(nr_str)
                if sched_id in self._running:
                    # Already running — track its next_run for sleep calculation
                    # (it may re-fire after the current run completes).
                    next_times.append(next_run)
                    continue
                if next_run <= now:
                    to_fire.append(sched_id)
                    self._running[sched_id] = None  # reserve slot before releasing lock
                else:
                    next_times.append(next_run)

        # Submit outside the lock — submit() is non-blocking but we minimise
        # lock hold time for the benefit of worker threads calling _load_schedules.
        for sched_id in to_fire:
            future = self._executor.submit(self._run_schedule, sched_id)
            with self._lock:
                if sched_id in self._running:  # wasn't immediately cancelled
                    self._running[sched_id] = future

        if next_times:
            soonest = min(next_times)
            return max(0.0, min((soonest - _utcnow()).total_seconds(), _TICK_CAP))
        return _TICK_CAP

    # ── schedule executor ─────────────────────────────────────────────────────

    def _run_schedule(self, sched_id: str) -> None:
        try:
            self._execute(sched_id)
        finally:
            with self._lock:
                self._running.pop(sched_id, None)

    def _execute(self, sched_id: str) -> None:
        # Load current entry
        with self._lock:
            data = _load_schedules()
        entry = next((e for e in data["schedules"] if e["id"] == sched_id), None)
        if entry is None:
            return

        settings       = entry.get("settings", {})
        remaining      = entry.get("remaining_loop_prompts") or settings.get("nextLoop_prompt_dynamic", [])
        session_id     = entry.get("session_id")
        continuing     = entry.get("continuing_from_limit_reached", False)
        allow_reschedule = entry.get("allow_reschedule", False)
        runs_remaining = entry.get("runs_remaining", 0)

        # Mark running
        with self._lock:
            data = _load_schedules()
            for e in data["schedules"]:
                if e["id"] == sched_id:
                    e["status"]   = "running"
                    e["last_run"] = _iso(_utcnow())
                    break
            _save_schedules(data)
        _write_loop_state("running", sched_id=sched_id)

        # ── blocking call ──────────────────────────────────────────────────
        try:
            result = run_loop(
                primary_prompt                = settings.get("primary_prompt", ""),
                primary_prompt_file           = settings.get("primary_prompt_file", "workflow.md"),
                allowed_tools                 = settings.get("allowed_tools"),
                nextLoop_prompt_static        = settings.get("nextLoop_prompt_static", ""),
                nextLoop_prompt_dynamic       = remaining,
                session_threshold             = settings.get("session_threshold", 80.0),
                weekly_threshold              = settings.get("weekly_threshold", 80.0),
                context_threshold             = settings.get("context_threshold", 90.0),
                session_id                    = session_id,
                allow_reschedule              = allow_reschedule,
                continuing_from_limit_reached = continuing,
            )
        except Exception as exc:
            with self._lock:
                data = _load_schedules()
                for e in data["schedules"]:
                    if e["id"] == sched_id:
                        e["status"] = "error"
                        e["last_result"] = {"success": 0, "limit_exceeded": 0}
                        break
                _save_schedules(data)
            _write_loop_state("error", sched_id=sched_id, error=f"run_loop raised: {exc}")
            return
        # ──────────────────────────────────────────────────────────────────

        # If cancelled while running: discard results and remove entry
        if sched_id in self._cancelled:
            with self._lock:
                self._cancelled.discard(sched_id)
                data = _load_schedules()
                data["schedules"] = [e for e in data["schedules"] if e["id"] != sched_id]
                _save_schedules(data)
            _write_loop_state("idle")
            return

        limit            = result.get("limit_exceeded", 0)
        success          = result.get("success", 0)
        rem              = result.get("remaining_loop_prompts", [])
        reschedule_time  = result.get("reschedule_time")
        new_session_id   = _read_last_session_id()

        new_status, next_run, new_runs_remaining = _compute_outcome(
            limit            = limit,
            success          = success,
            rem              = rem,
            reschedule_time  = reschedule_time,
            allow_reschedule = allow_reschedule,
            runs_remaining   = runs_remaining,
            entry            = entry,
        )

        # Persist outcome
        with self._lock:
            data = _load_schedules()
            for e in data["schedules"]:
                if e["id"] != sched_id:
                    continue
                e["status"]                      = new_status
                e["remaining_loop_prompts"]      = rem
                e["session_id"]                  = new_session_id
                e["continuing_from_limit_reached"] = (limit in (1, 3) and bool(rem))
                e["runs_remaining"]              = new_runs_remaining
                e["last_result"]                 = {"success": success, "limit_exceeded": limit}
                if next_run is not None:
                    e["next_run"] = _iso(next_run)
                break
            _save_schedules(data)

        error_msg = "run_loop returned success=0" if success == 0 else None
        _write_loop_state(new_status, sched_id=sched_id, error=error_msg)

        if new_status == "active":
            self._wakeup.set()  # recalculate sleep duration immediately

    # ── public API ────────────────────────────────────────────────────────────

    def schedule_loop(self, config: dict) -> str:
        """
        Primary entry point from the UI. Creates a schedule entry and either
        fires immediately (config["now"]=True) or waits for the next occurrence
        of hour_utc:minute_utc UTC.

        Required keys in config:
            primary_prompt           str
            nextLoop_prompt_static   str
            nextLoop_prompt_dynamic  list[str]  one entry per search; len = batch size

        Optional keys:
            now               bool   — fire immediately (default False)
            hour_utc          int    — UTC hour (required if now=False)
            minute_utc        int    — UTC minute (default 0)
            repeat            int    — 0=once, -1=forever, N=N additional runs
            allow_reschedule  bool   — auto-reschedule on session/API limit (default False)
            session_threshold float  — default 80.0
            weekly_threshold  float  — default 80.0
            context_threshold float  — default 90.0

        Returns the new schedule id (UUID4 string).
        """
        sched_id       = str(uuid.uuid4())
        now_dt         = _utcnow()
        fire_now: bool = config.get("now", False)
        hour:     int  = config.get("hour_utc", 0)
        minute:   int  = config.get("minute_utc", 0)
        repeat:   int  = config.get("repeat", 0)
        repeat_pattern: str = config.get("repeat_pattern", "daily")
        weekday_target: int = config.get("weekday", 0)

        if fire_now:
            next_run = now_dt
        else:
            next_run = _next_occurrence(hour, minute, repeat_pattern, weekday_target, now_dt)

        entry: dict = {
            "id":                          sched_id,
            "status":                      "active",
            "created":                     _iso(now_dt),
            "hour_utc":                    hour,
            "minute_utc":                  minute,
            "repeat":                      repeat,
            "repeat_pattern":              repeat_pattern,
            "weekday":                     weekday_target,
            "runs_remaining":              repeat,
            "next_run":                    _iso(next_run),
            "last_run":                    None,
            "session_id":                  config.get("session_id"),
            "remaining_loop_prompts":      config.get("remaining_loop_prompts", []),
            "continuing_from_limit_reached": config.get("continuing_from_limit_reached", False),
            "allow_reschedule":            config.get("allow_reschedule", False),
            "is_immediate":                fire_now,
            "settings": {
                "primary_prompt":          config.get("primary_prompt", ""),
                "primary_prompt_file":     config.get("primary_prompt_file", "workflow.md"),
                "allowed_tools":           config.get("allowed_tools"),
                "nextLoop_prompt_static":  config.get("nextLoop_prompt_static", ""),
                "nextLoop_prompt_dynamic": config.get("nextLoop_prompt_dynamic", []),
                "session_threshold":       config.get("session_threshold", 80.0),
                "weekly_threshold":        config.get("weekly_threshold", 80.0),
                "context_threshold":       config.get("context_threshold", 90.0),
            },
            "last_result": None,
        }

        with self._lock:
            data = _load_schedules()
            data["schedules"].append(entry)
            _save_schedules(data)

        self._wakeup.set()
        return sched_id

    def run_schedule_now(self, sched_id: str) -> bool:
        """
        Fire an existing schedule immediately, ignoring its next_run time.
        Returns False if the schedule is already running or not found.
        """
        with self._lock:
            if sched_id in self._running:
                return False
            data = _load_schedules()
            found = False
            for e in data["schedules"]:
                if e["id"] == sched_id:
                    e["next_run"] = _iso(_utcnow())
                    e["status"]   = "active"
                    found = True
                    break
            if not found:
                return False
            _save_schedules(data)
            future = self._executor.submit(self._run_schedule, sched_id)
            self._running[sched_id] = future
        return True

    def cancel_schedule(self, sched_id: str) -> bool:
        """
        Cancel a schedule. If currently running, marks it for cancellation —
        the worker discards its result and removes the entry when run_loop()
        returns (threads cannot be killed mid-call). If idle, removes immediately.
        Returns False if not found.
        """
        was_running = False
        with self._lock:
            data = _load_schedules()
            if not any(e["id"] == sched_id for e in data["schedules"]):
                return False
            if sched_id in self._running:
                self._cancelled.add(sched_id)
                was_running = True
            else:
                data["schedules"] = [e for e in data["schedules"] if e["id"] != sched_id]
                _save_schedules(data)
        if was_running:
            terminate_active()
        self._wakeup.set()
        return True

    def get_schedules(self) -> list:
        """
        Return all schedule entries from schedules.json annotated with:
            _is_running      bool — currently executing run_loop()
            _remaining_count int  — len(remaining_loop_prompts)
        """
        with self._lock:
            data        = _load_schedules()
            running_ids = set(self._running.keys())
        result = []
        for entry in data["schedules"]:
            e = dict(entry)
            e["_is_running"]      = entry["id"] in running_ids
            e["_remaining_count"] = len(entry.get("remaining_loop_prompts") or [])
            result.append(e)
        return result


# ── outcome computation (pure function, easier to unit-test) ──────────────────

def _compute_outcome(
    limit: int,
    success: int,
    rem: list,
    reschedule_time: Optional[str],
    allow_reschedule: bool,
    runs_remaining: int,
    entry: dict,
) -> tuple:
    """
    Given the result of a run_loop() call, return:
        (new_status: str, next_run: datetime | None, new_runs_remaining: int)

    runs_remaining semantics: "how many more runs to execute after this one."
    """
    # Limit cases take precedence over generic success=0 (run_loop returns success=0
    # when a limit is hit, but we must distinguish that from a true error).
    if limit in (1, 3) and reschedule_time and allow_reschedule:
        return ("active", _parse_iso(reschedule_time), runs_remaining)

    if limit in (1, 2, 3):
        return ("limit_reached", None, runs_remaining)

    # Not a limit — treat success=0 as an error
    if success == 0:
        return ("error", None, runs_remaining)

    # success=1, limit=0: all prompts completed this run — apply repeat logic
    if runs_remaining == 0:
        return ("completed", None, 0)

    # Compute next scheduled occurrence using the entry's repeat pattern
    next_run = _next_occurrence(
        entry.get("hour_utc", 0),
        entry.get("minute_utc", 0),
        entry.get("repeat_pattern", "daily"),
        entry.get("weekday", 0),
        _utcnow(),
    )

    if runs_remaining == -1:  # forever
        return ("active", next_run, -1)

    # runs_remaining > 0: schedule the next run, then decrement
    new_count = runs_remaining - 1
    if new_count > 0:
        return ("active", next_run, new_count)
    else:
        # new_count == 0: one more run is scheduled; it will see runs_remaining=0 → completed
        return ("active", next_run, 0)


# ── module-level singleton ────────────────────────────────────────────────────

_manager:      Optional[ScheduleManager] = None
_manager_lock: threading.Lock            = threading.Lock()


def get_manager() -> ScheduleManager:
    """Return the process-wide ScheduleManager, creating and starting it on first call."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ScheduleManager()
                _manager.start()
    return _manager
