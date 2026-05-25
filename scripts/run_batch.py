#!/usr/bin/env python3
"""
Batch runner — spawns the Claude workflow, tracks state, retries on context failure.
Writes run_state.json while running so the dashboard can poll status.
"""
import json
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE_PATH = ROOT / "run_state.json"
LOG_DIR = ROOT / "logs"

ALLOWED_TOOLS = (
    "Read,Write,Edit,Bash,Glob,Grep,TodoWrite,"
    "mcp__claude_ai_Indeed__search_jobs,mcp__claude_ai_Indeed__get_job_details"
)


# ── helpers ───────────────────────────────────────────────────────────────────

def read_state():
    # utf-8-sig strips BOM if present (guards against PowerShell Set-Content)
    return json.loads(STATE_PATH.read_text(encoding="utf-8-sig")) if STATE_PATH.exists() else {}


def write_state(data):
    STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def log(log_path, msg):
    """Append a timestamped [run_batch] line to the log file."""
    line = f"[run_batch] {datetime.now().strftime('%H:%M:%S')}  {msg}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


def is_pid_alive(pid):
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        else:
            import os
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def context_limit_likely(log_path):
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:].lower()
        return any(m in tail for m in
                   ["context length", "context limit", "token limit", "max_tokens", "rate limit"])
    except Exception:
        return False


# ── queue helpers ─────────────────────────────────────────────────────────────

def describe_batch():
    """Return a short human-readable summary of what will run."""
    try:
        from datetime import date
        data = json.loads(
            (ROOT / "search_queue.json").read_text(encoding="utf-8-sig"),
            object_pairs_hook=dict,
        )
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
        for e in sorted(queue, key=lambda e: e.get("id", 0)):
            if e.get("status") != "done" or e.get("skip_next"):
                continue
            lr = e.get("last_run")
            try:
                age = (today - date.fromisoformat(lr)).days if lr else rerun_days + 1
            except ValueError:
                age = rerun_days + 1
            if age > rerun_days:
                due.append(e)

        selected_p = pending[:batch_size]
        selected_d = due[:max(0, batch_size - len(selected_p))]
        total = len(selected_p) + len(selected_d)

        lines = [f"batch_size={batch_size}  rerun_after={rerun_days}d  "
                 f"queue={len(queue)} total  selected={total}"]
        for e in selected_p:
            lines.append(f"  [NEW]  {e.get('keyword')}  @  {e.get('location')}")
        for e in selected_d:
            lines.append(f"  [DUE]  {e.get('keyword')}  @  {e.get('location')}")
        if total == 0:
            lines.append("  (nothing to run — all searches are up to date)")
        return lines
    except Exception as exc:
        return [f"(could not read search_queue.json: {exc})"]


# ── Claude runner ─────────────────────────────────────────────────────────────

CLAUDE_FALLBACK_PATHS = [
    Path.home() / ".local" / "bin" / "claude.exe",
    Path.home() / ".local" / "bin" / "claude",
    Path("C:/Users") / Path.home().name / ".local" / "bin" / "claude.exe",
]


def find_claude():
    """Return path to the claude binary, checking PATH then common install locations."""
    found = shutil.which("claude")
    if found:
        return found
    for p in CLAUDE_FALLBACK_PATHS:
        if p.exists():
            return str(p)
    return None


def run_claude(log_path, extra_args=None):
    """Run claude with stdout/stderr redirected directly to the log file."""
    cmd = ["claude", "-p", str(ROOT / "workflow.md"), "--allowedTools", ALLOWED_TOOLS]
    if extra_args:
        cmd += extra_args

    claude_bin = find_claude()
    if not claude_bin:
        log(log_path, "ERROR: 'claude' not found in PATH or ~/.local/bin/")
        log(log_path, "       Install the Claude CLI: https://claude.ai/download")
        log(log_path, f"       sys.executable = {sys.executable}")
        return 1
    cmd[0] = claude_bin  # use absolute path to avoid PATH issues

    log(log_path, f"claude binary: {claude_bin}")
    log(log_path, f"cmd: {' '.join(str(c) for c in cmd)}")
    log(log_path, "─" * 60)

    def _pipe_reader(stream, path):
        """Read raw bytes from the pipe and append to log — runs in its own thread."""
        try:
            while True:
                chunk = stream.read(512)
                if not chunk:
                    break
                with open(path, "ab") as f:
                    f.write(chunk)
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except FileNotFoundError:
        log(log_path, "ERROR: 'claude' not found (FileNotFoundError)")
        return 1
    except Exception as exc:
        log(log_path, f"ERROR launching claude: {exc}")
        return 1

    state = read_state()
    state["pid"] = proc.pid
    write_state(state)
    log(log_path, f"claude PID: {proc.pid} — output follows")
    log(log_path, "")

    reader = threading.Thread(target=_pipe_reader, args=(proc.stdout, log_path), daemon=True)
    reader.start()
    rc = proc.wait()
    reader.join(timeout=10)

    log(log_path, "")
    log(log_path, "─" * 60)
    log(log_path, f"claude exited  returncode={rc}")
    return rc


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    state = read_state()

    # Prevent duplicate runs
    if state.get("status") == "running":
        pid = state.get("pid")
        if pid and is_pid_alive(pid):
            print(f"Batch already running (PID {pid})")
            sys.exit(1)
        print("Stale running state detected — clearing and starting fresh")

    started = datetime.now().isoformat(timespec="seconds")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"batch_{ts}.log"
    LOG_DIR.mkdir(exist_ok=True)

    # Write initial state
    write_state({
        "status": "running",
        "started": started,
        "ended": None,
        "pid": None,
        "attempt": 1,
        "log": log_path.name,
        "error": None,
    })

    # Open log and write pre-flight header
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"{'='*60}\n")
        f.write(f"Job Scout batch run started {started}\n")
        f.write(f"{'='*60}\n\n")

    log(log_path, f"python: {sys.executable}")
    log(log_path, f"cwd:    {ROOT}")
    log(log_path, "")
    log(log_path, "── Batch plan ──")
    for line in describe_batch():
        log(log_path, line)
    log(log_path, "")

    # First attempt
    log(log_path, "── Attempt 1 ──")
    rc = run_claude(log_path)

    # Retry on context/token limit
    if rc != 0 and context_limit_likely(log_path):
        retry_log = LOG_DIR / f"batch_{ts}_retry.log"
        log(log_path, "Context/token limit detected — will retry with --continue")
        write_state({
            "status": "running",
            "started": started,
            "ended": None,
            "pid": None,
            "attempt": 2,
            "log": retry_log.name,
            "error": f"attempt 1 exit {rc}, retrying",
        })
        with open(retry_log, "w", encoding="utf-8") as f:
            f.write(f"{'='*60}\n")
            f.write(f"Job Scout batch run RETRY  started {datetime.now().isoformat(timespec='seconds')}\n")
            f.write(f"{'='*60}\n\n")
        log(retry_log, "── Attempt 2 (--continue) ──")
        rc = run_claude(retry_log, extra_args=["--continue"])
        log_path = retry_log

    ended = datetime.now().isoformat(timespec="seconds")
    status = "done" if rc == 0 else "error"
    log(log_path, "")
    log(log_path, f"── Run complete  status={status}  returncode={rc}  ended={ended} ──")

    write_state({
        "status": status,
        "started": started,
        "ended": ended,
        "pid": None,
        "attempt": read_state().get("attempt", 1),
        "log": log_path.name,
        "returncode": rc,
        "error": None if rc == 0 else f"exited with code {rc}",
    })

    sys.exit(rc)


if __name__ == "__main__":
    main()
