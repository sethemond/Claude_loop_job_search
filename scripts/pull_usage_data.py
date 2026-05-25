#!/usr/bin/env python3
"""
Pull Claude Code usage data into Python variables for downstream use.

Primary path  : uses claude_monitor library if installed
                  pip install claude-code-usage-monitor
Fallback path : reads ~/.claude/projects/**/*.jsonl directly (no dependencies)

Run as a script to print a summary, or import and call get_usage() for structured data.

Exported structure (get_usage() return value):
  {
    "total_tokens":           int,
    "total_cost_usd":         float,
    "input_tokens":           int,
    "output_tokens":          int,
    "cache_creation_tokens":  int,
    "cache_read_tokens":      int,
    "session_count":          int,
    "entry_count":            int,
    "models_used":            list[str],
    "cost_by_model":          dict[str, float],
    "tokens_by_model":        dict[str, int],
    "sessions": [
      {
        "id":               str,
        "start_time":       datetime | str,
        "end_time":         datetime | str,
        "duration_minutes": float,
        "is_active":        bool,
        "total_tokens":     int,
        "cost_usd":         float,
        "models":           list[str],
        "input_tokens":     int,
        "output_tokens":    int,
        "cache_creation_tokens": int,
        "cache_read_tokens":     int,
        "messages_sent":    int,
      },
      ...
    ],
    "raw_entries": [            # only populated when library is NOT available
      { "timestamp", "input_tokens", "output_tokens", "cache_creation_tokens",
        "cache_read_tokens", "cost_usd", "model", "message_id", "request_id" },
      ...
    ],
    "source": "library" | "jsonl_direct",
    "hours_back": int,
  }
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_HOURS_BACK = 168  # 7 days


# ---------------------------------------------------------------------------
# Primary path — claude-monitor uv tool (subprocess into its isolated venv)
# ---------------------------------------------------------------------------

def _find_monitor_python() -> str | None:
    """Locate the Python interpreter inside the claude-monitor uv tool venv."""
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        r = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True, timeout=5)
        base = Path(r.stdout.strip())
        py = base / "claude-monitor" / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        return str(py) if py.exists() else None
    except Exception:
        return None


_MONITOR_PYTHON: str | None = _find_monitor_python()

# Inline script executed inside the tool's venv; args: hours_back
_MONITOR_FULL_SCRIPT = """\
import json, sys
from claude_monitor.data.analysis import analyze_usage
hours = int(sys.argv[1])
data = analyze_usage(hours_back=hours)
print(json.dumps(data, default=str))
"""


def _load_via_library(hours_back: int) -> dict[str, Any] | None:
    """Call analyze_usage via the uv tool venv Python; return structured data or None."""
    if not _MONITOR_PYTHON:
        return None
    try:
        r = subprocess.run(
            [_MONITOR_PYTHON, "-c", _MONITOR_FULL_SCRIPT, str(hours_back)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            print(f"[pull_usage_data] monitor call failed: {r.stderr.strip()[:200]}",
                  file=sys.stderr)
            return None
        data = json.loads(r.stdout)
    except Exception as exc:
        print(f"[pull_usage_data] monitor call error: {exc}", file=sys.stderr)
        return None

    raw_blocks = data.get("blocks", [])
    if not isinstance(raw_blocks, list):
        return None

    sessions: list[dict] = []
    total_input = total_output = total_cache_create = total_cache_read = 0
    total_cost = 0.0
    cost_by_model: dict[str, float] = defaultdict(float)
    tokens_by_model: dict[str, int] = defaultdict(int)
    all_models: set[str] = set()

    for blk in raw_blocks:
        tc   = blk.get("tokenCounts") or {}
        inp  = int(tc.get("inputTokens", 0))
        out  = int(tc.get("outputTokens", 0))
        cc   = int(tc.get("cacheCreationInputTokens", 0))
        cr   = int(tc.get("cacheReadInputTokens", 0))
        cost    = float(blk.get("costUSD", 0.0))
        models  = blk.get("models") or []
        tot_tok = int(blk.get("totalTokens", inp + out + cc + cr))
        dur_min = float(blk.get("durationMinutes", 0.0))
        is_act  = bool(blk.get("isActive", False))
        start   = blk.get("startTime")
        end     = blk.get("endTime")
        msgs    = int(blk.get("sentMessagesCount", 0))
        blk_id  = blk.get("id", "")

        total_input        += inp
        total_output       += out
        total_cache_create += cc
        total_cache_read   += cr
        total_cost         += cost
        all_models.update(models)

        for mdl in models:
            cost_by_model[mdl]   += cost / max(len(models), 1)
            tokens_by_model[mdl] += tot_tok // max(len(models), 1)

        sessions.append({
            "id":                    blk_id,
            "start_time":            start,
            "end_time":              end,
            "duration_minutes":      float(dur_min),
            "is_active":             bool(is_act),
            "total_tokens":          int(tot_tok),
            "cost_usd":              float(cost),
            "models":                list(models),
            "input_tokens":          int(inp),
            "output_tokens":         int(out),
            "cache_creation_tokens": int(cc),
            "cache_read_tokens":     int(cr),
            "messages_sent":         int(msgs),
        })

    return {
        "total_tokens":           total_input + total_output + total_cache_create + total_cache_read,
        "total_cost_usd":         total_cost,
        "input_tokens":           total_input,
        "output_tokens":          total_output,
        "cache_creation_tokens":  total_cache_create,
        "cache_read_tokens":      total_cache_read,
        "session_count":          len(sessions),
        "entry_count":            int(data.get("entries_count", 0)),
        "models_used":            sorted(all_models),
        "cost_by_model":          dict(cost_by_model),
        "tokens_by_model":        dict(tokens_by_model),
        "sessions":               sessions,
        "raw_entries":            [],
        "source":                 "library",
        "hours_back":             hours_back,
    }


# ---------------------------------------------------------------------------
# Fallback path — read JSONL directly
# ---------------------------------------------------------------------------

_CLAUDE_DATA_DIR = Path.home() / ".claude" / "projects"


def _parse_timestamp(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_model(entry: dict) -> str:
    for key in ("model", "modelId", "model_id"):
        if entry.get(key):
            return str(entry[key])
    msg = entry.get("message") or {}
    for key in ("model", "modelId"):
        if msg.get(key):
            return str(msg[key])
    return ""


def _extract_cost(entry: dict) -> float:
    for key in ("cost_usd", "costUsd", "cost"):
        val = entry.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return 0.0


def _load_jsonl_direct(hours_back: int, data_dir: Path = _CLAUDE_DATA_DIR) -> dict[str, Any]:
    cutoff: datetime | None = None
    if hours_back > 0:
        cutoff = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    seen_ids: set[str] = set()
    raw_entries: list[dict] = []

    if not data_dir.exists():
        print(f"[pull_usage_data] data dir not found: {data_dir}", file=sys.stderr)
    else:
        for jsonl_file in sorted(data_dir.rglob("*.jsonl")):
            try:
                text = jsonl_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue

                ts = _parse_timestamp(entry.get("timestamp"))
                if cutoff and ts and ts.tzinfo and ts < cutoff:
                    continue

                msg_id = (
                    entry.get("message_id")
                    or entry.get("messageId")
                    or (entry.get("message") or {}).get("id")
                    or ""
                )
                req_id = entry.get("request_id") or entry.get("requestId") or ""
                dedup_key = f"{msg_id}|{req_id}"
                if dedup_key != "|" and dedup_key in seen_ids:
                    continue
                seen_ids.add(dedup_key)

                usage = entry.get("usage") or entry.get("message", {}).get("usage") or {}
                raw_entries.append({
                    "timestamp":             ts,
                    "input_tokens":          int(usage.get("input_tokens", 0)),
                    "output_tokens":         int(usage.get("output_tokens", 0)),
                    "cache_creation_tokens": int(usage.get("cache_creation_input_tokens", usage.get("cache_creation_tokens", 0))),
                    "cache_read_tokens":     int(usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0))),
                    "cost_usd":              _extract_cost(entry),
                    "model":                 _extract_model(entry),
                    "message_id":            msg_id,
                    "request_id":            req_id,
                })

    # Aggregate
    total_input = sum(e["input_tokens"]          for e in raw_entries)
    total_output = sum(e["output_tokens"]         for e in raw_entries)
    total_cc     = sum(e["cache_creation_tokens"] for e in raw_entries)
    total_cr     = sum(e["cache_read_tokens"]     for e in raw_entries)
    total_cost   = sum(e["cost_usd"]              for e in raw_entries)

    cost_by_model: dict[str, float] = defaultdict(float)
    tokens_by_model: dict[str, int] = defaultdict(int)
    for e in raw_entries:
        mdl = e["model"] or "unknown"
        cost_by_model[mdl]   += e["cost_usd"]
        tokens_by_model[mdl] += (e["input_tokens"] + e["output_tokens"]
                                 + e["cache_creation_tokens"] + e["cache_read_tokens"])

    return {
        "total_tokens":           total_input + total_output + total_cc + total_cr,
        "total_cost_usd":         total_cost,
        "input_tokens":           total_input,
        "output_tokens":          total_output,
        "cache_creation_tokens":  total_cc,
        "cache_read_tokens":      total_cr,
        "session_count":          0,
        "entry_count":            len(raw_entries),
        "models_used":            sorted(set(e["model"] for e in raw_entries if e["model"])),
        "cost_by_model":          dict(cost_by_model),
        "tokens_by_model":        dict(tokens_by_model),
        "sessions":               [],
        "raw_entries":            raw_entries,
        "source":                 "jsonl_direct",
        "hours_back":             hours_back,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_usage(hours_back: int = DEFAULT_HOURS_BACK) -> dict[str, Any]:
    """
    Return a dict of usage data pulled from Claude Code's local data files.
    Tries the claude_monitor library first; falls back to direct JSONL parsing.
    """
    data = _load_via_library(hours_back)
    if data is not None:
        return data
    return _load_jsonl_direct(hours_back)


# ---------------------------------------------------------------------------
# Convenience top-level variables (populated on import)
# ---------------------------------------------------------------------------

_usage = get_usage()

total_tokens:           int         = _usage["total_tokens"]
total_cost_usd:         float       = _usage["total_cost_usd"]
input_tokens:           int         = _usage["input_tokens"]
output_tokens:          int         = _usage["output_tokens"]
cache_creation_tokens:  int         = _usage["cache_creation_tokens"]
cache_read_tokens:      int         = _usage["cache_read_tokens"]
session_count:          int         = _usage["session_count"]
entry_count:            int         = _usage["entry_count"]
models_used:            list        = _usage["models_used"]
cost_by_model:          dict        = _usage["cost_by_model"]
tokens_by_model:        dict        = _usage["tokens_by_model"]
sessions:               list        = _usage["sessions"]
raw_entries:            list        = _usage["raw_entries"]
data_source:            str         = _usage["source"]


# ---------------------------------------------------------------------------
# CLI summary
# ---------------------------------------------------------------------------

def _fmt_tokens(n: int) -> str:
    return f"{n:,}"


def print_summary(data: dict | None = None) -> None:
    d = data or _usage
    hrs = d["hours_back"]
    print(f"\n{'='*54}")
    print(f"  Claude Code Usage  (last {hrs}h / {hrs//24}d)")
    print(f"{'='*54}")
    print(f"  Source       : {d['source']}")
    print(f"  Entries      : {d['entry_count']:,}")
    if d['session_count']:
        print(f"  Sessions     : {d['session_count']:,}")
    print(f"  Total tokens : {_fmt_tokens(d['total_tokens'])}")
    print(f"    Input      : {_fmt_tokens(d['input_tokens'])}")
    print(f"    Output     : {_fmt_tokens(d['output_tokens'])}")
    print(f"    Cache write: {_fmt_tokens(d['cache_creation_tokens'])}")
    print(f"    Cache read : {_fmt_tokens(d['cache_read_tokens'])}")
    print(f"  Total cost   : ${d['total_cost_usd']:.4f}")
    if d["models_used"]:
        print(f"  Models used  : {', '.join(d['models_used'])}")
    if d["cost_by_model"]:
        print(f"  Cost by model:")
        for mdl, cost in sorted(d["cost_by_model"].items(), key=lambda x: -x[1]):
            print(f"    {mdl:<40} ${cost:.4f}")
    if d["sessions"]:
        active = [s for s in d["sessions"] if s["is_active"]]
        print(f"  Active session: {'YES — ' + str(active[0]['models']) if active else 'none'}")
    print(f"{'='*54}\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Print Claude Code usage summary")
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS_BACK,
                        help="Hours of history to include (default: 168 = 7 days)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    result = get_usage(hours_back=args.hours)

    if args.json:
        # datetime objects aren't JSON-serializable — convert to ISO strings
        def _serial(obj: Any) -> str:
            if isinstance(obj, datetime):
                return obj.isoformat()
            raise TypeError(f"Not serializable: {type(obj)}")
        print(json.dumps(result, default=_serial, indent=2))
    else:
        print_summary(result)
