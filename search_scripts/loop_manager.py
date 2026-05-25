"""
loop_manager.py — Multi-turn Claude prompt loop orchestrator.

Manages sequential Claude calls across a list of dynamic prompts, checking usage
and context thresholds between each call and logging all activity. Callable
programmatically via run_loop() or directly from the CLI.

─── INPUTS ───────────────────────────────────────────────────────────────────

primary_prompt           [str]       Prompt sent on the first call of a new session.
                                     Combined with static and the first dynamic prompt.

nextLoop_prompt_static   [str]       Fixed text prepended to every loop's prompt.
                                     Applied on every call (new session and resumed).

nextLoop_prompt_dynamic  [list[str]] Per-loop prompt list. One Claude call is made
                                     per entry; list length determines number of loops.

session_threshold        [float]     Stop if session block usage reaches this percent
                                     (0–100). Default: 80.

weekly_threshold         [float]     Stop if 7-day rolling usage reaches this percent
                                     (0–100). Default: 80.

context_threshold        [float]     If the active session's context length exceeds
                                     this percent, reset to a new session before the
                                     next call. Default: 90.

session_id               [str|None]  Existing Claude session UUID to resume.
                                     If None, a new session is created.

allow_reschedule         [bool]      If True and a threshold is exceeded, populate
                                     reschedule_time in the return value. Default: False.

loop_log_file            [str|None]  Path for the orchestration log.
                                     Default: logs/loop_log_<YYYYMMDD_HHMMSS>.txt

claude_log_file          [str|None]  Path for Claude stream output logs.
                                     Default (recommended): auto-generated per loop as
                                     logs/loop_log_<ts>_claude_<n>.txt, which groups
                                     them with their loop log in directory listings.

continuing_from_limit_reached [bool] Set True when restarting after a previous run
                                     was interrupted mid-loop by a hard limit.
                                     Default: False.

continue_prompt          [str|None]  Prompt sent on the first call when
                                     continuing_from_limit_reached is True. Replaces
                                     the normal prompt to resume where Claude left off.
                                     Only used on the first call; subsequent calls are
                                     normal. Required if continuing_from_limit_reached
                                     is True; falls back to static+dynamic if omitted.

─── OUTPUTS ──────────────────────────────────────────────────────────────────

success                  int         1 = all loops completed or graceful threshold stop.
                                     0 = Claude returned an unexpected error.

limit_exceeded           int         0 = no limit hit.
                                     1 = session usage threshold reached (before call).
                                     2 = weekly usage threshold reached (before call).
                                     3 = unexpected API limit hit inside Claude.

remaining_loop_prompts   list[str]   Unprocessed entries from nextLoop_prompt_dynamic.
                                     Non-empty when the loop stopped early; pass these
                                     back as nextLoop_prompt_dynamic on the next run.

reschedule_time          str|None    ISO UTC timestamp of the next session-block reset,
                                     or None if allow_reschedule=False or no limit hit.

─── PROMPT CONSTRUCTION ──────────────────────────────────────────────────────

New session (session_id=None):
    primary_prompt + nextLoop_prompt_static + dynamic[i]

Resumed session (session_id provided, context below threshold):
    nextLoop_prompt_static + dynamic[i]

Context reset (context_threshold exceeded mid-loop):
    session_id is cleared; next call uses the new-session form above.

Continuing from limit (continuing_from_limit_reached=True, first call only):
    continue_prompt                           ← replaces the normal prompt
    Subsequent loops revert to resumed-session form.

─── LOOP PROCESS (all steps logged to loop_log_file) ─────────────────────────

Determine session mode: new / resume / continue-from-limit

For each prompt in nextLoop_prompt_dynamic:
  [1] Query session usage  → stop (limit_exceeded=1) if session_pct >= session_threshold
  [2] Query weekly usage   → stop (limit_exceeded=2) if weekly_pct >= weekly_threshold
  [3] Check context length → if context_pct >= context_threshold, reset session_id to None
  [4] Read jobs.json       → record jobs_before count
  [5] Build prompt         → based on session mode (see above)
  [6] Call claude_call.run_claude() → returns (status, session_id, context_tokens)
  [7] Read jobs.json       → jobs_after; jobs_new = jobs_after - jobs_before
  [8] Query post-call usage for cost figures
  [9] Append record to runs.json (started, ended, status, costs, job delta, session_id)
  [10] If status == LIMITS  → stop (limit_exceeded=3); populate reschedule_time if allowed
       If status == ERROR   → stop (success=0)
       Otherwise            → advance to next dynamic prompt

Return result dict.
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path regardless of how this script is invoked.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import argparse
import json
from datetime import datetime

from search_scripts.claude_call import run_claude, STATUS_OK, STATUS_LIMITS, STATUS_ERROR
from search_scripts.check_claude_usage import get_usage_snapshot
from search_scripts.context_usage import get_context_usage
from search_scripts.count_jobs import get_job_count


_RUNS_PATH   = _ROOT / "runs.json"
_QUEUE_PATH  = _ROOT / "search_queue.json"
_LOGS_DIR    = _ROOT / "logs"


# ── helpers ───────────────────────────────────────────────────────────────────

def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log(log_file, message: str) -> None:
    line = f"[{_now_str()}] {message}\n"
    log_file.write(line)
    log_file.flush()
    print(line, end="")


def _build_prompt(*parts: str) -> str:
    """Join non-empty prompt parts with newlines."""
    return "\n".join(p for p in parts if p and p.strip())


def _append_run_record(record: dict) -> None:
    """Append one record to the runs array in runs.json."""
    try:
        if _RUNS_PATH.exists():
            with open(_RUNS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "runs" in data:
                data["runs"].append(record)
            elif isinstance(data, list):
                data.append(record)
            else:
                data = {"runs": [record]}
        else:
            data = {"runs": [record]}
        with open(_RUNS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        print(f"WARNING: could not write to runs.json: {exc}", file=sys.stderr)


# ── core function ─────────────────────────────────────────────────────────────

def _parse_kw_loc(dynamic_prompt: str) -> "tuple[str, str]":
    """Parse 'keyword: X, location: Y' into (keyword, location).

    Splits on ', location:' first so locations containing commas
    (e.g. 'Charleston, SC') are preserved intact.
    """
    sep = ", location:"
    if sep in dynamic_prompt:
        kw_part, loc = dynamic_prompt.split(sep, 1)
        kw = kw_part.split(":", 1)[1].strip() if ":" in kw_part else kw_part.strip()
        return kw, loc.strip()
    # Fallback for prompts not matching the expected format
    parts: dict = {}
    for seg in dynamic_prompt.split(","):
        if ":" in seg:
            k, v = seg.split(":", 1)
            parts[k.strip().lower()] = v.strip()
    return parts.get("keyword", ""), parts.get("location", "")


def _mark_queue_entry_done(keyword: str, location: str, timestamp: str) -> None:
    """Mark the matching search_queue entry as done with a last_run timestamp."""
    if not keyword and not location:
        return
    try:
        if not _QUEUE_PATH.exists():
            return
        with open(_QUEUE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        changed = False
        for entry in data.get("queue", []):
            if (entry.get("keyword", "").strip() == keyword
                    and entry.get("location", "").strip() == location):
                entry["status"]   = "done"
                entry["last_run"] = timestamp
                changed = True
                break
        if changed:
            with open(_QUEUE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except Exception as exc:
        print(f"WARNING: could not update search_queue.json: {exc}", file=sys.stderr)


def run_loop(
    primary_prompt: str,
    nextLoop_prompt_static: str,
    nextLoop_prompt_dynamic: "list[str]",
    primary_prompt_file: str = "workflow.md",
    allowed_tools: "str | None" = None,
    session_threshold: float = 80.0,
    weekly_threshold: float = 80.0,
    context_threshold: float = 90.0,
    session_id: "str | None" = None,
    allow_reschedule: bool = False,
    loop_log_file: "str | None" = None,
    claude_log_file: "str | None" = None,
    continuing_from_limit_reached: bool = False,
    continue_prompt: "str | None" = None,
) -> dict:
    """
    Run the Claude prompt loop.

    Returns a dict with keys:
        success                 — 1 on success, 0 on error
        limit_exceeded          — 0=none, 1=session threshold, 2=weekly threshold,
                                  3=unexpected API limit from Claude
        remaining_loop_prompts  — unprocessed dynamic prompts
        reschedule_time         — ISO UTC string when session resets, or None
    """
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    loop_log_path = Path(loop_log_file) if loop_log_file else _LOGS_DIR / f"loop_log_{run_ts}.txt"
    loop_log_path.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "success": 1,
        "limit_exceeded": 0,
        "remaining_loop_prompts": list(nextLoop_prompt_dynamic),
        "reschedule_time": None,
    }

    dynamic = list(nextLoop_prompt_dynamic)
    is_first_call = True

    with open(loop_log_path, "a", encoding="utf-8") as lf:
        _log(lf, "=" * 60)
        _log(lf, f"LOOP START  ts={run_ts}  loops={len(dynamic)}  session={session_id or 'new'}")
        _log(lf, f"primary_prompt_file={primary_prompt_file}")
        _log(lf, (
            f"session_threshold={session_threshold}%  "
            f"weekly_threshold={weekly_threshold}%  "
            f"context_threshold={context_threshold}%"
        ))
        if continuing_from_limit_reached:
            _log(lf, "Mode: continuing from interrupted session")
        elif session_id:
            _log(lf, "Mode: resuming existing session")
        else:
            _log(lf, "Mode: new session")

        if not dynamic:
            _log(lf, "No dynamic prompts provided — nothing to do")
            _log(lf, "LOOP END  success=1  limit_exceeded=0")
            _log(lf, "=" * 60)
            return result

        for i, dynamic_prompt in enumerate(dynamic):
            _log(lf, f"--- Loop {i + 1}/{len(dynamic)} ---")

            # [1] Check session usage
            snapshot = get_usage_snapshot()
            if not snapshot.get("ok"):
                _log(lf, f"WARNING: usage unavailable: {snapshot.get('error')}")
            else:
                sess_pct  = snapshot["session_pct"]
                week_pct  = snapshot["weekly_pct"]
                sess_cost = snapshot["session_cost"]
                week_cost = snapshot["weekly_cost"]
                _log(lf, (
                    f"Usage — session: {sess_pct}% (${sess_cost:.4f})  "
                    f"weekly: {week_pct}% (${week_cost:.4f})"
                ))

                if sess_pct >= session_threshold:
                    _log(lf, f"SESSION threshold exceeded ({sess_pct}% >= {session_threshold}%) — stopping")
                    result["limit_exceeded"] = 1
                    result["remaining_loop_prompts"] = dynamic[i:]
                    if allow_reschedule:
                        result["reschedule_time"] = snapshot.get("session_end_utc")
                    break

                # [2] Check weekly usage
                if week_pct >= weekly_threshold:
                    _log(lf, f"WEEKLY threshold exceeded ({week_pct}% >= {weekly_threshold}%) — stopping")
                    result["limit_exceeded"] = 2
                    result["remaining_loop_prompts"] = dynamic[i:]
                    break

            # [3] Check context (only when there is an active session to examine)
            if session_id:
                ctx = get_context_usage(session_id)
                ctx_pct = ctx.get("pct", 0)
                _log(lf, f"Context: {ctx_pct}% ({ctx.get('context', '?')} tokens)")
                if ctx_pct >= context_threshold:
                    _log(lf, (
                        f"CONTEXT threshold exceeded ({ctx_pct}% >= {context_threshold}%) "
                        "— starting fresh session"
                    ))
                    session_id = None

            # [4] Job count before
            jobs_before = get_job_count()
            _log(lf, f"jobs_before={jobs_before}")

            # [5] Build prompt
            _log(lf, f"dynamic_prompt: {dynamic_prompt}")
            if is_first_call and continuing_from_limit_reached:
                if continue_prompt:
                    prompt = continue_prompt
                    _log(lf, "Using continue_prompt (interrupted session recovery)")
                else:
                    _log(lf, "WARNING: continuing_from_limit_reached=True but continue_prompt not provided — using standard prompt")
                    prompt = _build_prompt(nextLoop_prompt_static, dynamic_prompt)
            elif session_id is None:
                prompt = _build_prompt(primary_prompt, nextLoop_prompt_static, dynamic_prompt)
                _log(lf, f"Prompt: full ({primary_prompt_file} + static + dynamic)")
            else:
                prompt = _build_prompt(nextLoop_prompt_static, dynamic_prompt)
                _log(lf, "Prompt: continuation (static + dynamic)")

            # Resolve per-iteration claude log path.
            # Default: loop_log_<ts>_claude_<n>.txt so files sort together in the directory.
            if claude_log_file:
                claude_log_path = claude_log_file
            else:
                loop_stem = loop_log_path.stem  # e.g. "loop_log_20260523_142913"
                claude_log_path = str(loop_log_path.parent / f"{loop_stem}_claude_{i + 1}.txt")

            _log(lf, f"Claude log: {claude_log_path}  session={session_id or 'new'}")

            # [6] Claude call
            call_start = _iso_now()
            status, new_session_id, context_tokens = run_claude(prompt, claude_log_path, session_id, allowed_tools)
            call_end = _iso_now()

            session_id   = new_session_id
            is_first_call = False

            _log(lf, (
                f"Claude done — status={status}  "
                f"session={session_id}  context_tokens={context_tokens}"
            ))

            # [7] Job count after
            jobs_after = get_job_count()
            _log(lf, f"jobs_after={jobs_after}  jobs_new={jobs_after - jobs_before}")

            # [8] Post-call usage snapshot for the runs.json record
            post_snap = get_usage_snapshot()
            sess_cost_post = post_snap.get("session_cost", 0.0) if post_snap.get("ok") else 0.0
            week_cost_post = post_snap.get("weekly_cost", 0.0) if post_snap.get("ok") else 0.0

            # [9] Append record to runs.json
            run_status = (
                "success"      if status == STATUS_OK     else
                "limit_reached" if status == STATUS_LIMITS else
                "error"
            )
            _kw, _loc = _parse_kw_loc(dynamic_prompt)
            _append_run_record({
                "started":             call_start,
                "ended":               call_end,
                "status":              run_status,
                "loop_index":          i,
                "session_id":          session_id,
                "session_cost":        sess_cost_post,
                "weekly_cost":         week_cost_post,
                "jobs_before":         jobs_before,
                "jobs_after":          jobs_after,
                "jobs_new":            jobs_after - jobs_before,
                "primary_prompt_file": primary_prompt_file,
                "keyword":             _kw,
                "location":            _loc,
                "dynamic_prompt":      dynamic_prompt,
                "errors":              [],
            })

            # [10] Handle non-success Claude statuses
            if status == STATUS_LIMITS:
                _log(lf, "Unexpected API limit hit inside Claude — stopping")
                result["limit_exceeded"] = 3
                result["remaining_loop_prompts"] = dynamic[i + 1:]
                if allow_reschedule:
                    fresh = get_usage_snapshot()
                    result["reschedule_time"] = fresh.get("session_end_utc") if fresh.get("ok") else None
                break

            if status == STATUS_ERROR:
                _log(lf, "Claude call returned an error — stopping loop")
                result["success"] = 0
                result["remaining_loop_prompts"] = dynamic[i + 1:]
                break

            # Successful iteration — mark queue entry done, consume this dynamic prompt
            _mark_queue_entry_done(_kw, _loc, call_end)
            result["remaining_loop_prompts"] = dynamic[i + 1:]

        _log(lf, (
            f"LOOP END  success={result['success']}  "
            f"limit_exceeded={result['limit_exceeded']}  "
            f"remaining={len(result['remaining_loop_prompts'])}"
        ))
        _log(lf, "=" * 60)

    return result


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Multi-turn Claude prompt loop orchestrator.\n\n"
            "Output JSON fields:\n"
            "  success                 1=ok, 0=error\n"
            "  limit_exceeded          0=none, 1=session, 2=weekly, 3=unexpected API limit\n"
            "  remaining_loop_prompts  unprocessed dynamic prompts\n"
            "  reschedule_time         ISO UTC reset time, or null\n\n"
            "Examples:\n"
            "  python search_scripts/loop_manager.py \\\n"
            '    --primary-prompt "Search Indeed for engineering jobs" \\\n'
            '    --static-prompt  "Continue the job search." \\\n'
            '    --dynamic-prompts "keyword=mechanical, location=Charleston SC" \\\n'
            '                      "keyword=robotics, location=Austin TX"\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--primary-prompt",  required=True,  help="Prompt for the first loop / new session")
    parser.add_argument("--static-prompt",   required=True,  help="Static prompt appended on every loop")
    parser.add_argument("--dynamic-prompts", required=True,  nargs="+", metavar="PROMPT",
                        help="One or more per-loop dynamic prompts (quoted). Number of prompts = number of loops.")
    parser.add_argument("--session-threshold",  type=float, default=80.0, help="Session usage %% cutoff (default: 80)")
    parser.add_argument("--weekly-threshold",   type=float, default=80.0, help="Weekly usage %% cutoff (default: 80)")
    parser.add_argument("--context-threshold",  type=float, default=90.0, help="Context length %% cutoff (default: 90)")
    parser.add_argument("--session-id",         default=None, help="Existing Claude session ID to resume")
    parser.add_argument("--allow-reschedule",   action="store_true", help="Return reschedule_time when limits hit")
    parser.add_argument("--loop-log-file",      default=None, help="Path for the loop log (default: logs/loop_log_<ts>.txt)")
    parser.add_argument("--claude-log-file",    default=None, help="Path for Claude output log (default: per-loop in logs/)")
    parser.add_argument("--continuing-from-limit", action="store_true",
                        help="Set when resuming after a previous run was interrupted by a limit")
    parser.add_argument("--continue-prompt",    default=None,
                        help="Prompt to send on first call when --continuing-from-limit is set")
    args = parser.parse_args()

    result = run_loop(
        primary_prompt                = args.primary_prompt,
        nextLoop_prompt_static        = args.static_prompt,
        nextLoop_prompt_dynamic       = args.dynamic_prompts,
        session_threshold             = args.session_threshold,
        weekly_threshold              = args.weekly_threshold,
        context_threshold             = args.context_threshold,
        session_id                    = args.session_id,
        allow_reschedule              = args.allow_reschedule,
        loop_log_file                 = args.loop_log_file,
        claude_log_file               = args.claude_log_file,
        continuing_from_limit_reached = args.continuing_from_limit,
        continue_prompt               = args.continue_prompt,
    )

    print(json.dumps(result, indent=2), flush=True)
    sys.exit(0 if result["success"] == 1 and result["limit_exceeded"] == 0 else 1)


if __name__ == "__main__":
    main()
