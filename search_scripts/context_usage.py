#!/usr/bin/env python3
"""
context_usage.py — Return current context token usage for a Claude session.

Public API:
    from search_scripts.context_usage import get_context_usage

    result = get_context_usage("345f2a06-88f2-4d0f-a14d-457aa35e36a9")
    # {
    #   "session_id":     "345f2a06-...",
    #   "context":        24351,
    #   "input_tokens":   3,
    #   "output_tokens":  12,
    #   "cache_read":     16235,
    #   "cache_creation": 8101,
    #   "method":         "jsonl",
    #   "jsonl_path":     "C:\\...",
    # }
    # On failure: {"error": "<reason>"}
"""

from __future__ import annotations

import json
from pathlib import Path


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _find_session_jsonl(session_id: str) -> Path | None:
    if not CLAUDE_PROJECTS_DIR.exists():
        return None
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def _read_jsonl_context(jsonl_path: Path) -> dict:
    last_usage = None
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                usage = (
                    obj.get("message", {}).get("usage")
                    if isinstance(obj.get("message"), dict)
                    else None
                ) or obj.get("usage")

                if not isinstance(usage, dict):
                    continue

                inp = int(usage.get("input_tokens", 0))
                out = int(usage.get("output_tokens", 0))
                if inp or out:
                    last_usage = {
                        "input_tokens":   inp,
                        "output_tokens":  out,
                        "cache_read":     int(usage.get("cache_read_input_tokens", 0)),
                        "cache_creation": int(usage.get("cache_creation_input_tokens", 0)),
                    }
    except OSError as exc:
        return {"error": str(exc)}

    if last_usage is None:
        return {"error": "no usage data found in JSONL"}

    total = (
        last_usage["input_tokens"]
        + last_usage["cache_read"]
        + last_usage["cache_creation"]
        + last_usage["output_tokens"]
    )
    return {**last_usage, "context": total, "pct": round(total / 200_000 * 100, 2)}


def get_context_usage(session_id: str) -> dict:
    """
    Return context token usage for the given Claude session UUID.

    Reads ~/.claude/projects/**/<session_id>.jsonl and returns stats from
    the most recent assistant turn.

    Returns a dict with keys:
        session_id, context, input_tokens, output_tokens,
        cache_read, cache_creation, method, jsonl_path
    On failure returns {"error": "<reason>"}.
    """
    jsonl_path = _find_session_jsonl(session_id)
    if not jsonl_path:
        return {"error": f"no JSONL found for session {session_id}"}

    result = _read_jsonl_context(jsonl_path)
    if "error" not in result:
        result["session_id"] = session_id
        result["jsonl_path"] = str(jsonl_path)
        result["method"] = "jsonl"
    return result
