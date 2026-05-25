#!/usr/bin/env python3
"""Restart the dashboard server. Usage: python scripts/restart_server.py [port]"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000


def kill_existing():
    if sys.platform == "win32":
        # Find PIDs listening on the port
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{PORT}" in line and "LISTENING" in line:
                parts = line.split()
                pid = parts[-1]
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               capture_output=True, timeout=5)
                print(f"Killed PID {pid} (was on port {PORT})")
    else:
        subprocess.run(["pkill", "-f", "serve.py"], capture_output=True)


def start_server():
    serve = ROOT / "scripts" / "serve.py"
    if sys.platform == "win32":
        proc = subprocess.Popen(
            [sys.executable, str(serve), str(PORT)],
            cwd=str(ROOT),
            creationflags=subprocess.DETACHED_PROCESS,
        )
    else:
        proc = subprocess.Popen(
            [sys.executable, str(serve), str(PORT)],
            cwd=str(ROOT),
            start_new_session=True,
        )
    return proc.pid


def wait_for_ready(timeout=10):
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("localhost", PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


if __name__ == "__main__":
    print(f"Restarting Job Scout server on port {PORT}...")
    kill_existing()
    time.sleep(0.5)
    pid = start_server()
    print(f"Started (PID {pid}), waiting for port {PORT}...")
    if wait_for_ready():
        print(f"Server ready -> http://localhost:{PORT}")
    else:
        print("Warning: server did not respond within 10s — check logs")
    sys.exit(0)
