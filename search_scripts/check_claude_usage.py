#!/usr/bin/env python3
"""
check_claude_usage.py — Claude session & weekly usage snapshot
==============================================================

PURPOSE
-------
Queries claude-monitor for the current session usage, weekly (7-day) usage,
and estimated time until the active session block resets.  Returns a dict
silently by default (intended for programmatic use), a formatted text report
with --text, or raw JSON with --json.

REQUIREMENTS
------------
claude-monitor must be installed in a uv tool venv:
    uv tool install claude-monitor

HOW IT WORKS
------------
Locates the Python interpreter inside the claude-monitor uv venv, then runs
two short inline scripts to pull data from analyze_usage():
  - Session query  — active block cost, message count, and reset timestamp
  - Weekly query   — sum of all block costs over the last 168 hours (7 days)

Plan cost limits are hardcoded from Anthropic billing docs and used to
compute percentage-of-limit figures.  Pass --plan to select your tier.

USAGE EXAMPLES
--------------
    python scripts/check_claude_usage.py
    python scripts/check_claude_usage.py --plan max5
    python scripts/check_claude_usage.py --text
    python scripts/check_claude_usage.py --json
    python scripts/check_claude_usage.py --plan max20 --json
    python scripts/check_claude_usage.py --help

EXIT CODES
----------
    0  success
    1  monitor unavailable or query failed
    2  invalid arguments
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── plan cost limits ──────────────────────────────────────────────────────────
# Source: claude-monitor plans.py / Anthropic billing documentation.
# session_usd — maximum USD cost per 5-hour rolling session block.
# weekly_usd  — maximum USD cost per rolling 7-day window.
PLAN_LIMITS: dict[str, dict[str, float]] = {
    "pro":    {"session_usd": 18.00,  "weekly_usd": 111.0},
    "max5":   {"session_usd": 35.00,  "weekly_usd": 216.0},
    "max20":  {"session_usd": 140.00, "weekly_usd": 864.0},
    "custom": {"session_usd": 50.00,  "weekly_usd":   0.0},
}

# ── inline queries run inside the claude-monitor venv ─────────────────────────

# Returns cost, message count, reset time, AND the dynamic P90 cost limit for
# the active block.  Uses hours_back=192 (same as claude-monitor's DataManager)
# so there is enough history to compute the P90 from limit-hit sessions.
# The P90 limit is computed via AdvancedCustomLimitDisplay — the same code path
# claude-monitor's UI uses when plan="custom" (its default).
# For non-custom plans the static per-plan cost_limit is returned instead.
_SESSION_QUERY = """\
import json
from claude_monitor.data.analysis import analyze_usage
from claude_monitor.ui.components import AdvancedCustomLimitDisplay
from claude_monitor.core.plans import get_cost_limit

data  = analyze_usage(hours_back=192)
block = next((b for b in data['blocks'] if b.get('isActive') and not b.get('isGap')), None)

# Dynamic P90 limit — mirrors claude-monitor display_controller.py lines 236-243
temp = AdvancedCustomLimitDisplay(None)
sd   = temp._collect_session_data(data['blocks'])
pct  = temp._calculate_session_percentiles(sd['limit_sessions'])
cost_limit_p90 = pct['costs']['p90']

print(json.dumps({
    'cost_usd':        block['costUSD']          if block else 0.0,
    'messages':        block['sentMessagesCount'] if block else 0,
    'session_end_utc': block['endTime']           if block else None,
    'cost_limit_p90':  cost_limit_p90,
}))
"""

# Returns total cost summed across all non-gap blocks in the last 7 days.
_WEEKLY_QUERY = """\
import json
from claude_monitor.data.analysis import analyze_usage
data   = analyze_usage(hours_back=168)
blocks = [b for b in data['blocks'] if not b.get('isGap')]
print(json.dumps({'cost_usd': sum(b['costUSD'] for b in blocks)}))
"""


# ── discovery ─────────────────────────────────────────────────────────────────

def find_monitor_python() -> str | None:
    """Locate the Python interpreter inside the claude-monitor uv tool venv.

    Finds `uv` on PATH, asks it for the tool directory, then resolves
    the Python binary inside the claude-monitor sub-environment.

    Returns:
        Absolute path to the interpreter as a string, or None if uv or the
        venv cannot be found.
    """
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        r = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        base = Path(r.stdout.strip())
        py_rel = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        py = base / "claude-monitor" / py_rel
        return str(py) if py.exists() else None
    except Exception:
        return None


# ── query runner ──────────────────────────────────────────────────────────────

def run_monitor_query(monitor_python: str, query: str) -> dict | None:
    """Execute an inline Python snippet inside the claude-monitor venv.

    The snippet must write a single JSON object to stdout.

    Args:
        monitor_python: absolute path to the venv's Python interpreter.
        query: Python source code to run with -c.

    Returns:
        Parsed dict on success, or None on subprocess failure, timeout,
        non-zero exit, or JSON parse error.
    """
    try:
        r = subprocess.run(
            [monitor_python, "-c", query],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


# ── formatting helpers ────────────────────────────────────────────────────────

def seconds_to_hms(seconds: float) -> str:
    """Convert a duration in seconds to a 'Xh YYm ZZs' string."""
    seconds = max(0, int(seconds))
    h, rem  = divmod(seconds, 3600)
    m, s    = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def format_pct(value: float, limit: float) -> str:
    """Format 'X.X% of $Y.YY', or a 'N/A' note when limit is zero."""
    if limit <= 0:
        return "N/A (no limit configured)"
    return f"{value / limit:.1%} of ${limit:.2f}"


# ── core snapshot ─────────────────────────────────────────────────────────────

def get_usage_snapshot(plan: str = "custom") -> dict:
    """Query claude-monitor for current session and weekly usage.

    Args:
        plan: Claude plan tier — 'pro', 'max5', 'max20', or 'custom' (default).
              'custom' uses a dynamic P90 cost limit computed from historical
              sessions that hit limits — the same method claude-monitor's UI uses.
              Static-plan values fall back to 'pro' limits if unknown.

    Returns:
        dict with keys:
            ok              — True when at least session data was retrieved.
            error           — Human-readable failure reason, or None.
            plan            — Plan name actually used.
            session_cost    — Active session cost in USD.
            session_limit   — Plan session cost cap in USD.
            session_pct     — session usage as a percentage, 0–100 scale, rounded to
                              one decimal (e.g. 27.9 means 27.9%). 0.0 when no limit.
            session_msgs    — Messages sent in the active block.
            session_end_utc — ISO UTC timestamp when the session block resets,
                              or None if no block is active.
            time_to_reset   — Seconds until reset, or None if unknown.
            weekly_cost     — Total cost over the last 7 days in USD.
            weekly_limit    — Plan weekly cost cap in USD.
            weekly_pct      — weekly usage as a percentage, 0–100 scale, rounded to
                              one decimal (e.g. 49.7 means 49.7%). 0.0 when no limit.
    """
    plan_key = plan.lower()
    limits   = PLAN_LIMITS.get(plan_key, PLAN_LIMITS["pro"])
    # Custom plan has no defined weekly cap; fall back to Pro's weekly limit
    # so weekly percentage is still meaningful.
    week_lim = limits["weekly_usd"] or PLAN_LIMITS["pro"]["weekly_usd"]

    result: dict = {
        "ok":              False,
        "error":           None,
        "plan":            plan_key,
        "session_cost":    0.0,
        "session_limit":   0.0,   # filled in below (static or P90)
        "session_pct":     0.0,
        "session_msgs":    0,
        "session_end_utc": None,
        "time_to_reset":   None,
        "weekly_cost":     0.0,
        "weekly_limit":    week_lim,
        "weekly_pct":      0.0,
    }

    monitor_py = find_monitor_python()
    if not monitor_py:
        result["error"] = (
            "claude-monitor not found — install with: uv tool install claude-monitor"
        )
        return result

    # ── session block ─────────────────────────────────────────────────────────
    sess_data = run_monitor_query(monitor_py, _SESSION_QUERY)
    if sess_data is None:
        result["error"] = (
            "session query failed — claude-monitor may be outdated or its venv is broken"
        )
        return result

    sess_cost = float(sess_data.get("cost_usd", 0.0))
    sess_msgs = int(sess_data.get("messages", 0))
    sess_end  = sess_data.get("session_end_utc")   # ISO UTC str or None

    # For custom plan, use the dynamic P90 limit from historical limit-hit sessions
    # (same method as claude-monitor's display_controller.py).  For all other plans,
    # use the static per-plan dollar cap.
    if plan_key == "custom":
        sess_lim = float(sess_data.get("cost_limit_p90") or 0.0)
    else:
        sess_lim = limits["session_usd"]

    result["session_cost"]  = sess_cost
    result["session_limit"] = sess_lim
    result["session_msgs"]  = sess_msgs
    result["session_end_utc"] = sess_end
    result["session_pct"]   = round(sess_cost / sess_lim * 100, 1) if sess_lim > 0 else 0.0

    # Compute time-to-reset from the block's end timestamp
    if sess_end:
        try:
            end_dt    = datetime.fromisoformat(str(sess_end).replace("Z", "+00:00"))
            remaining = (end_dt - datetime.now(timezone.utc)).total_seconds()
            result["time_to_reset"] = max(0.0, remaining)
        except Exception:
            pass  # leave time_to_reset as None

    # ── weekly cost ───────────────────────────────────────────────────────────
    week_data = run_monitor_query(monitor_py, _WEEKLY_QUERY)
    if week_data is not None:
        week_cost = float(week_data.get("cost_usd", 0.0))
        result["weekly_cost"] = week_cost
        result["weekly_pct"]  = round(week_cost / week_lim * 100, 1) if week_lim > 0 else 0.0

    result["ok"] = True
    return result


# ── formatted output ──────────────────────────────────────────────────────────

def print_report(snapshot: dict) -> None:
    """Print a human-readable usage report to stdout."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sep = "=" * 52

    print(f"\n{sep}")
    print(f"  Claude Usage Snapshot  —  {now}")
    print(sep)

    if not snapshot["ok"]:
        print(f"  ERROR: {snapshot['error']}")
        print(f"{sep}\n")
        return

    print(f"  Plan : {snapshot['plan'].upper()}")
    print()

    # Session block
    sess_cost = snapshot["session_cost"]
    sess_lim  = snapshot["session_limit"]
    sess_msgs = snapshot["session_msgs"]
    sess_end  = snapshot["session_end_utc"]
    ttr       = snapshot["time_to_reset"]

    print("  SESSION BLOCK")
    print(f"    Cost      : ${sess_cost:.4f}  ({format_pct(sess_cost, sess_lim)})")
    print(f"    Messages  : {sess_msgs}")
    if sess_end:
        print(f"    Resets at : {sess_end}")
        if ttr is not None:
            print(f"    Time left : {seconds_to_hms(ttr)}")
        else:
            print("    Time left : (could not calculate)")
    else:
        print("    Resets at : (no active block — session already reset or idle)")
    print()

    # Weekly
    week_cost = snapshot["weekly_cost"]
    week_lim  = snapshot["weekly_limit"]

    print("  WEEKLY  (rolling 7 days)")
    print(f"    Cost      : ${week_cost:.4f}  ({format_pct(week_cost, week_lim)})")
    print()
    print(sep + "\n")


# ── argument parsing ──────────────────────────────────────────────────────────

def parse_args(argv: list[str]) -> tuple[str, str] | None:
    """Parse command-line arguments.

    Flags:
        --plan {pro,max5,max20,custom}   Plan tier (default: pro).
        --text                           Emit a formatted text report.
        --json                           Emit raw JSON.
        --help / -h                      Print docstring and exit 0.

    Returns:
        (plan, output_mode) where output_mode is 'dict', 'text', or 'json'.
        Returns None after printing an error to stderr on invalid input.
    """
    plan        = "custom"   # matches claude-monitor's default
    output_mode = "dict"     # default

    args = argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]

        if arg in ("--help", "-h"):
            print(__doc__)
            sys.exit(0)

        elif arg == "--plan":
            i += 1
            if i >= len(args):
                print("ERROR: --plan requires a value  (e.g. --plan max5)", file=sys.stderr)
                return None
            plan = args[i].lower()
            if plan not in PLAN_LIMITS:
                valid = ", ".join(PLAN_LIMITS)
                print(
                    f"ERROR: unknown plan '{args[i]}' — valid options: {valid}",
                    file=sys.stderr,
                )
                return None

        elif arg == "--text":
            output_mode = "text"

        elif arg == "--json":
            output_mode = "json"

        else:
            print(f"ERROR: unrecognised argument '{arg}'", file=sys.stderr)
            print(
                "Usage: python scripts/check_claude_usage.py [--plan PLAN] [--text | --json]",
                file=sys.stderr,
            )
            return None

        i += 1

    return plan, output_mode


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> dict | None:
    """Entry point — parse args, query monitor, print results, and return the snapshot dict.

    Calling examples:
        python scripts/check_claude_usage.py
            Returns dict silently — intended for import/programmatic use.

        python scripts/check_claude_usage.py --plan max5
            Returns dict with max5 session ($35) and weekly ($216) limits.

        python scripts/check_claude_usage.py --text
            Formatted human-readable text report.

        python scripts/check_claude_usage.py --json
            Raw JSON — useful for scripting or piping to jq.

        python scripts/check_claude_usage.py --plan max20 --json
            Raw JSON with max20 limits.

        python scripts/check_claude_usage.py --plan invalid
            Exits 2 with a clear error message (input validation failure).

        python scripts/check_claude_usage.py --help
            Prints this module's docstring and exits 0.

    Exit codes:
        0  success
        1  monitor unavailable or query failed
        2  invalid arguments
    """
    parsed = parse_args(sys.argv)
    if parsed is None:
        sys.exit(2)

    plan, output_mode = parsed
    snapshot = get_usage_snapshot(plan)

    if output_mode == "json":
        print(json.dumps(snapshot, indent=2))
    elif output_mode == "text":
        print_report(snapshot)

    return snapshot


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result and result.get("ok") else 1)
