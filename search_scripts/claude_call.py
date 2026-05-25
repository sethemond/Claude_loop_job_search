#!/usr/bin/env python3
"""
claude_call.py — Standalone Claude CLI invocation with stream-json parsing.

Usage:
    python search_scripts/claude_call.py <prompt> <log_file> [session_id]

Arguments:
    prompt      Text prompt to send to Claude
    log_file    Path to output log file (appended if exists, created if not)
    session_id  (optional) Existing Claude session ID to resume

Output (JSON printed to stdout):
    status      0 = unknown error, 1 = success, 2 = limits reached
    session_id  Claude session UUID used or created during this run
    context     Total token count (input + output) from final usage stats

Exit codes:
    0  Success (status=1)
    1  Limits reached (status=2) or error (status=0)
    2  Bad arguments (prompt or log_file empty / missing)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path


# ── status codes ──────────────────────────────────────────────────────────────

STATUS_ERROR  = 0  # unknown or unexpected failure
STATUS_OK     = 1  # completed successfully
STATUS_LIMITS = 2  # usage, context, or rate limit was hit


# ── active process tracking ───────────────────────────────────────────────────

_active_proc: "subprocess.Popen | None" = None
_active_proc_lock = threading.Lock()


def terminate_active() -> bool:
    """Kill the currently running Claude subprocess, if any. Returns True if a process was killed."""
    global _active_proc
    with _active_proc_lock:
        proc = _active_proc
    if proc is None or proc.poll() is not None:
        return False
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return True


# ── constants ─────────────────────────────────────────────────────────────────

# Default tool allowlist passed to every Claude invocation via --allowedTools.
# Override by passing `allowed_tools` to run_claude(), or propagate from the UI
# through build_loop_config → schedule_manager → run_loop → run_claude.
ALLOWED_TOOLS = (
    "Read,Write,Edit,Bash,Glob,Grep,TodoWrite,"
    "mcp__claude_ai_Indeed__search_jobs,mcp__claude_ai_Indeed__get_job_details"
)

# Substrings that indicate a usage or context limit was reached in Claude output.
LIMIT_KEYWORDS = [
    "hit your session limit",
    "context length",
    "context limit",
    "token limit",
    "max_tokens",
    "rate limit",
]

# Checked in order when `claude` is not on PATH.
CLAUDE_FALLBACK_PATHS = [
    Path.home() / ".local" / "bin" / "claude.exe",
    Path.home() / ".local" / "bin" / "claude",
]


# ── helpers ───────────────────────────────────────────────────────────────────

def now_str() -> str:
    """Current timestamp formatted for log lines."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_line(log_file, message: str) -> None:
    """Write a timestamped line to the log file and echo to stdout."""
    line = f"[{now_str()}] {message}\n"
    log_file.write(line)
    log_file.flush()
    print(line, end="")


def emit_raw(log_file, text: str) -> None:
    """Write raw text (no timestamp) to log and stdout — used for streaming content deltas."""
    log_file.write(text)
    log_file.flush()
    print(text, end="", flush=True)


def find_claude() -> str | None:
    """Locate the claude CLI binary. Checks PATH first, then common fallback paths."""
    found = shutil.which("claude")
    if found:
        return found
    for path in CLAUDE_FALLBACK_PATHS:
        if path.exists():
            return str(path)
    return None


def contains_limit_signal(text: str) -> bool:
    """Return True if text contains any known limit/rate-limit keyword."""
    lowered = text.lower()
    return any(kw in lowered for kw in LIMIT_KEYWORDS)


# ── stream-json parser ────────────────────────────────────────────────────────

def parse_stream(proc: subprocess.Popen, log_file) -> tuple[str | None, bool, int]:
    """
    Consume and parse stream-json lines from proc.stdout.

    Reads one JSON event per line, writes formatted output to log_file,
    and tracks limit signals and usage statistics.

    Event types handled:
        system/init          — captures session_id and model name
        stream_event         — content blocks (thinking, text, tool_use) and their deltas
        tool_result          — logs a truncated preview of tool output
        result               — final summary: captures usage, detects limit signals
        system/api_retry     — flags rate_limit or max_output_tokens as a limit hit

    Args:
        proc      — running Claude subprocess with stdout=PIPE
        log_file  — open writable file handle for log output

    Returns:
        session_id  — Claude session UUID (None if not received before stream ended)
        limit_hit   — True if any limit condition was detected in the stream
        context     — total tokens (input + output) from the most recent usage stats
    """
    session_id         = None
    limit_hit          = False
    context            = 0
    current_block_type = None  # tracks which content block is currently streaming

    for raw_line in proc.stdout:
        raw_line = raw_line.rstrip("\n")
        if not raw_line:
            continue

        # Parse JSON event; if not valid JSON, log the raw line and continue.
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            log_line(log_file, raw_line)
            continue

        etype   = event.get("type", "")
        subtype = event.get("subtype", "")

        # Session initialization — log session ID and model.
        if etype == "system" and subtype == "init":
            session_id = event.get("session_id")
            log_line(log_file, f"[SESSION] id={session_id}  model={event.get('model', '?')}")

        # Content streaming — thinking, text, and tool_use blocks with deltas.
        elif etype == "stream_event":
            inner = event.get("event", {})
            itype = inner.get("type", "")

            if itype == "content_block_start":
                blk = inner.get("content_block", {})
                current_block_type = blk.get("type")
                if current_block_type == "thinking":
                    emit_raw(log_file, f"\n[{now_str()}] [THINKING] ")
                elif current_block_type == "text":
                    emit_raw(log_file, f"\n[{now_str()}] [CLAUDE] ")
                elif current_block_type == "tool_use":
                    emit_raw(log_file, f"\n[{now_str()}] [TOOL] {blk.get('name', '?')} → ")

            elif itype == "content_block_delta":
                delta = inner.get("delta", {})
                dtype = delta.get("type", "")
                if dtype == "thinking_delta":
                    emit_raw(log_file, delta.get("thinking", ""))
                elif dtype == "text_delta":
                    emit_raw(log_file, delta.get("text", ""))

            elif itype == "content_block_stop":
                if current_block_type in ("thinking", "text", "tool_use"):
                    emit_raw(log_file, "\n")
                current_block_type = None

            elif itype == "message_start":
                # Capture context size from the opening message usage (updated later by result).
                usage = inner.get("message", {}).get("usage", {})
                if usage:
                    context = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))

        # Tool result — log a truncated preview of what the tool returned.
        elif etype == "tool_result":
            content = event.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    c.get("text", "")[:300] for c in content if c.get("type") == "text"
                ).strip()
            else:
                text = str(content)[:300].strip()
            if text:
                log_line(log_file, f"[TOOL RESULT] {text[:300]}{'…' if len(text) > 300 else ''}")

        # Final result event — update session ID, usage, and detect limit signals.
        elif etype == "result":
            session_id  = event.get("session_id") or session_id
            result_text = event.get("result", "").strip()
            cost        = event.get("total_cost_usd") or event.get("cost_usd")
            turns       = event.get("num_turns", "?")

            # Final usage is the most accurate context count — overwrite message_start value.
            usage = event.get("usage", {})
            if usage:
                context = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))

            log_line(log_file, f"[RESULT] subtype={subtype}  turns={turns}  cost=${cost}")
            if result_text:
                log_line(log_file, f"[RESULT TEXT] {result_text[:400]}")

            # Check result text for limit signals regardless of subtype.
            if result_text and contains_limit_signal(result_text):
                limit_hit = True
                log_line(log_file, "[LIMIT] detected in result text")

        # API retry — rate_limit and max_output_tokens are hard limit signals.
        elif etype == "system" and subtype == "api_retry":
            error = event.get("error", "")
            log_line(log_file, f"[API RETRY] attempt={event.get('attempt')}  error={error}")
            if error in ("rate_limit", "max_output_tokens"):
                limit_hit = True
                log_line(log_file, f"[LIMIT] api_retry signals limit: error={error}")

    return session_id, limit_hit, context


# ── core function ─────────────────────────────────────────────────────────────

def run_claude(
    prompt: str,
    log_path: str,
    session_id: "str | None" = None,
    allowed_tools: "str | None" = None,
) -> "tuple[int, str | None, int]":
    """
    Launch the Claude CLI, stream output to a log file, and return results.

    Args:
        prompt        — text to send to Claude via `-p`
        log_path      — path to log file (appended if exists, created with dirs if not)
        session_id    — existing session UUID to resume via `--resume`; None for new session
        allowed_tools — comma-separated tool allowlist for --allowedTools; None → ALLOWED_TOOLS

    Returns:
        (status, session_id, context)
          status     — STATUS_OK (1), STATUS_LIMITS (2), or STATUS_ERROR (0)
          session_id — session UUID from the run (may be None on hard launch failures)
          context    — total tokens (input + output) from final usage; 0 if unavailable
    """
    claude_bin = find_claude()
    if not claude_bin:
        print("ERROR: claude binary not found in PATH or common install paths", file=sys.stderr)
        return STATUS_ERROR, None, 0

    log_file_path = Path(log_path)
    try:
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: cannot create log directory '{log_file_path.parent}': {exc}", file=sys.stderr)
        return STATUS_ERROR, None, 0

    tools = allowed_tools if allowed_tools is not None else ALLOWED_TOOLS
    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--allowedTools", tools,
    ]
    if session_id:
        cmd += ["--resume", session_id]

    started = datetime.now().isoformat(timespec="seconds")

    with open(log_file_path, "a", encoding="utf-8") as log_file:
        log_line(log_file, "=" * 60)
        log_line(log_file, f"RUN START  {started}  session={session_id or 'new'}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as exc:
            log_line(log_file, f"ERROR: failed to launch claude: {exc}")
            log_line(log_file, "=" * 60)
            return STATUS_ERROR, None, 0

        global _active_proc
        with _active_proc_lock:
            _active_proc = proc
        try:
            out_session_id, limit_hit, context = parse_stream(proc, log_file)
            proc.stdout.close()
            proc.wait()
        finally:
            with _active_proc_lock:
                _active_proc = None

        ended            = datetime.now().isoformat(timespec="seconds")
        final_session_id = out_session_id or session_id

        if limit_hit:
            status = STATUS_LIMITS
        elif proc.returncode == 0:
            status = STATUS_OK
        else:
            status = STATUS_ERROR

        log_line(log_file, (
            f"RUN END  status={status}  rc={proc.returncode}"
            f"  session={final_session_id}  context={context}  ended={ended}"
        ))
        log_line(log_file, "=" * 60)
        log_file.write("\n")

    return status, final_session_id, context


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone Claude CLI wrapper.\n"
            "Runs a prompt, streams output to a log file, and prints results as JSON.\n\n"
            "Output JSON fields:\n"
            "  status      0=error  1=success  2=limits reached\n"
            "  session_id  Claude session UUID\n"
            "  context     Total tokens used (input + output)\n\n"
            "example:\n"
            " python search_scripts/claude_call.py \"your prompt here\" \"logs/my_run.log\" \n"
            " python search_scripts/claude_call.py \"follow-up prompt\" \"logs/my_run.log\" <existing_session_id>\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "prompt",
        help="Text prompt to send to Claude",
    )
    parser.add_argument(
        "log_file",
        help="Path to log file. Appended if it exists; created (with parent dirs) if not.",
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help="(optional) Existing Claude session ID to resume. Omit to start a new session.",
    )
    args = parser.parse_args()

    # Validate inputs — fail fast with a clear JSON error instead of a runtime crash.
    if not args.prompt.strip():
        print(json.dumps({
            "status": STATUS_ERROR, "session_id": None, "context": 0,
            "error": "prompt must not be empty",
        }), flush=True)
        sys.exit(2)

    if not args.log_file.strip():
        print(json.dumps({
            "status": STATUS_ERROR, "session_id": None, "context": 0,
            "error": "log_file path must not be empty",
        }), flush=True)
        sys.exit(2)

    status, session_id, context = run_claude(args.prompt, args.log_file, args.session_id)

    result = {
        "status":     status,      # 0=error, 1=success, 2=limits
        "session_id": session_id,
        "context":    context,
    }
    print(json.dumps(result, indent=2), flush=True)

    # Exit 0 on success, 1 on any failure or limit.
    sys.exit(0 if status == STATUS_OK else 1)


if __name__ == "__main__":
    main()
