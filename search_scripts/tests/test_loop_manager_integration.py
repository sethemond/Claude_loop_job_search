#!/usr/bin/env python3
"""
test_loop_manager_integration.py — Real-world integration tests for loop_manager.py

These tests make ACTUAL Claude calls — no mocking.  Keep prompts short.
All logs persist after the run in logs/integration/<timestamp>/ for inspection.
Runs records go to logs/integration/runs_integration.json (not runs.json).

What is verified:
  Test 1 — New session: Claude called, session_id captured, log has correct markers
  Test 2 — Multi-loop sequential: 3 prompts in order, session propagated across loops
  Test 3 — Session resume: second run_loop call resumes session from first
  Test 4 — Continue-from-limit: recovery prompt used on first call only

Run:
    python search_scripts/tests/test_loop_manager_integration.py
"""

import json
import shutil
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from search_scripts.loop_manager import run_loop
from search_scripts.claude_call import find_claude


# ── test environment setup ────────────────────────────────────────────────────

_RUN_TS   = datetime.now().strftime("%Y%m%d_%H%M%S")
_INT_DIR  = _ROOT / "logs" / "integration" / _RUN_TS
_RUNS_FILE = _ROOT / "logs" / "integration" / "runs_integration.json"


def _init_dirs() -> None:
    _INT_DIR.mkdir(parents=True, exist_ok=True)
    _RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _RUNS_FILE.exists():
        _RUNS_FILE.write_text(json.dumps({"runs": []}))


def _read_runs() -> list:
    with open(_RUNS_FILE, encoding="utf-8") as f:
        return json.load(f)["runs"]


def _log_content(path: str | Path) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def _claude_available() -> bool:
    return find_claude() is not None


# ── base class ────────────────────────────────────────────────────────────────

@unittest.skipUnless(_claude_available(), "claude binary not found on PATH")
class IntegrationBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _init_dirs()
        print(f"\n  Logs: {_INT_DIR}")
        print(f"  Runs: {_RUNS_FILE}")

    def setUp(self):
        self._count_before = len(_read_runs())

    def _call(self, **kwargs) -> dict:
        """Run run_loop with logs/runs redirected to the integration directory."""
        with patch("search_scripts.loop_manager._RUNS_PATH", _RUNS_FILE), \
             patch("search_scripts.loop_manager._LOGS_DIR",  _INT_DIR):
            return run_loop(**kwargs)

    def _new_runs(self, since: int | None = None) -> list:
        start = since if since is not None else self._count_before
        return _read_runs()[start:]

    def _extract_claude_log_paths(self, loop_log_path: str) -> list[str]:
        """Parse the loop log to find which claude log files were used per loop."""
        paths = []
        for line in _log_content(loop_log_path).splitlines():
            if "Claude log:" in line:
                after = line.split("Claude log:", 1)[1]
                log_path = after.split("  session=")[0].strip()
                paths.append(log_path)
        return paths


# ── Test 1: single loop, new session ─────────────────────────────────────────

class Test1NewSession(IntegrationBase):
    """
    One dynamic prompt with no session_id.

    Checks:
    - run_loop returns success=1, limit_exceeded=0, remaining=[]
    - One record written to runs_integration.json with a non-empty session_id
    - Claude log file exists and contains expected markers:
        RUN START  session=new
        [SESSION] id=<uuid>
        [CLAUDE]
        RUN END
    """

    def test_new_session_single_loop(self):
        loop_log   = str(_INT_DIR / "t1_loop.txt")
        claude_log = str(_INT_DIR / "t1_claude.txt")

        result = self._call(
            primary_prompt        = "You are a one-sentence test assistant.",
            nextLoop_prompt_static = "Reply with a single short sentence only.",
            nextLoop_prompt_dynamic= ["Reply with: INTEGRATION_T1_OK"],
            session_id            = None,
            loop_log_file         = loop_log,
            claude_log_file       = claude_log,
        )

        # Return value
        self.assertEqual(result["success"],               1,  "success must be 1")
        self.assertEqual(result["limit_exceeded"],        0,  "no limit should be hit")
        self.assertEqual(result["remaining_loop_prompts"], [], "all prompts consumed")

        # runs.json record
        new = self._new_runs()
        self.assertEqual(len(new), 1, "exactly one record written")
        rec = new[0]
        self.assertEqual(rec["status"],     "success")
        self.assertEqual(rec["loop_index"], 0)
        self.assertIsNotNone(rec["session_id"])
        self.assertTrue(rec["session_id"].strip(), "session_id must not be blank")

        # Claude log content
        log = _log_content(claude_log)
        self.assertTrue(Path(claude_log).exists(), "claude log file must exist")
        self.assertIn("session=new",  log, "first call must start a new session")
        self.assertIn("[SESSION]",    log, "log must contain [SESSION] marker")
        self.assertIn("[CLAUDE]",     log, "log must contain [CLAUDE] output")
        self.assertIn("RUN END",      log, "log must contain RUN END")

        print(f"\n  session_id : {rec['session_id']}")
        print(f"  claude log : {claude_log}")


# ── Test 2: multi-loop sequential, session propagated ─────────────────────────

class Test2MultiLoopSequential(IntegrationBase):
    """
    Three dynamic prompts in a single run_loop call.

    Checks:
    - All 3 loops complete in order (3 records, loop_index 0/1/2)
    - Loop 0 claude log: session=new
    - Loop 1 and 2 claude logs: session=<uuid> (resumed, not new)
    - All 3 records share the same session_id
    - Each claude log file is distinct and contains [CLAUDE] output
    """

    def test_multi_loop_session_propagation(self):
        loop_log = str(_INT_DIR / "t2_loop.txt")

        result = self._call(
            primary_prompt        = "You are a one-sentence test assistant.",
            nextLoop_prompt_static = "Reply with a single short sentence only.",
            nextLoop_prompt_dynamic= [
                "Reply with: LOOP_ONE",
                "Reply with: LOOP_TWO",
                "Reply with: LOOP_THREE",
            ],
            session_id            = None,
            loop_log_file         = loop_log,
            # No claude_log_file → auto-generated per loop (default behavior)
        )

        self.assertEqual(result["success"],               1)
        self.assertEqual(result["limit_exceeded"],        0)
        self.assertEqual(result["remaining_loop_prompts"], [])

        # Records
        new = self._new_runs()
        self.assertEqual(len(new), 3, "three records — one per loop")

        for i, rec in enumerate(new):
            self.assertEqual(rec["loop_index"], i,        f"loop {i}: wrong loop_index")
            self.assertEqual(rec["status"],    "success", f"loop {i}: wrong status")
            self.assertIsNotNone(rec["session_id"],       f"loop {i}: session_id is None")
            self.assertTrue(rec["session_id"].strip(),    f"loop {i}: session_id is blank")

        session_ids = [r["session_id"] for r in new]
        print(f"\n  session_ids: {session_ids}")
        self.assertEqual(
            len(set(session_ids)), 1,
            f"all loops must share one session_id; got: {set(session_ids)}"
        )

        # Claude log files — extract paths from loop log
        claude_logs = self._extract_claude_log_paths(loop_log)
        self.assertEqual(len(claude_logs), 3, "loop log must reference 3 claude log files")
        self.assertEqual(len(set(claude_logs)), 3, "each loop must use a distinct log file")

        print(f"  claude logs: {claude_logs}")

        # Loop 0 → new session
        log0 = _log_content(claude_logs[0])
        self.assertIn("session=new",  log0, "loop 0 must start a new session")
        self.assertIn("[SESSION]",    log0, "loop 0 log must have [SESSION] marker")
        self.assertIn("[CLAUDE]",     log0, "loop 0 log must have Claude output")

        # Loop 1 → resumed session
        log1 = _log_content(claude_logs[1])
        self.assertNotIn("session=new", log1, "loop 1 must NOT start a new session")
        self.assertIn("session=",       log1, "loop 1 must reference a session UUID")
        self.assertIn("[SESSION]",      log1, "loop 1 log must have [SESSION] marker")
        self.assertIn("[CLAUDE]",       log1, "loop 1 log must have Claude output")

        # Loop 2 → still resumed
        log2 = _log_content(claude_logs[2])
        self.assertNotIn("session=new", log2, "loop 2 must NOT start a new session")
        self.assertIn("[CLAUDE]",       log2, "loop 2 log must have Claude output")


# ── Test 3: session resume across two separate run_loop calls ─────────────────

class Test3CrossCallResume(IntegrationBase):
    """
    Two separate run_loop calls.  Second call explicitly passes session_id from first.

    Checks:
    - First call: claude log says session=new, session_id is captured
    - Second call: claude log says session=<uuid> (NOT new), containing the exact UUID
    """

    def test_cross_call_session_resume(self):
        loop_log_1   = str(_INT_DIR / "t3a_loop.txt")
        loop_log_2   = str(_INT_DIR / "t3b_loop.txt")
        claude_log_1 = str(_INT_DIR / "t3a_claude.txt")
        claude_log_2 = str(_INT_DIR / "t3b_claude.txt")

        # ── Call 1: new session ───────────────────────────────────────────────
        result_1 = self._call(
            primary_prompt        = "You are a one-sentence test assistant.",
            nextLoop_prompt_static = "Reply with a single short sentence only.",
            nextLoop_prompt_dynamic= ["Reply with: PART_ONE"],
            session_id            = None,
            loop_log_file         = loop_log_1,
            claude_log_file       = claude_log_1,
        )

        self.assertEqual(result_1["success"], 1, "first call must succeed")
        runs_1 = self._new_runs()
        self.assertEqual(len(runs_1), 1)
        captured_session = runs_1[0]["session_id"]
        self.assertIsNotNone(captured_session, "must capture session_id from first call")

        log1 = _log_content(claude_log_1)
        self.assertIn("session=new", log1, "first call must be a new session")
        print(f"\n  captured session_id: {captured_session}")

        # ── Call 2: resume ────────────────────────────────────────────────────
        count_before_2 = len(_read_runs())
        result_2 = self._call(
            primary_prompt        = "You are a one-sentence test assistant.",
            nextLoop_prompt_static = "Reply with a single short sentence only.",
            nextLoop_prompt_dynamic= ["Reply with: PART_TWO"],
            session_id            = captured_session,
            loop_log_file         = loop_log_2,
            claude_log_file       = claude_log_2,
        )

        self.assertEqual(result_2["success"], 1, "second call must succeed")
        runs_2 = self._new_runs(since=count_before_2)
        self.assertEqual(len(runs_2), 1)

        log2 = _log_content(claude_log_2)
        self.assertNotIn(
            "session=new", log2,
            f"second call must resume, not start new.\nLog preview:\n{log2[:600]}"
        )
        self.assertIn(
            captured_session, log2,
            "second call log must contain the exact captured session_id"
        )
        self.assertIn("[CLAUDE]", log2, "second call log must have Claude output")

        print(f"  call 1 log: {claude_log_1}")
        print(f"  call 2 log: {claude_log_2}")


# ── Test 4: continue-from-limit recovery ──────────────────────────────────────

class Test4ContinueFromLimit(IntegrationBase):
    """
    Simulates recovering from an interrupted session.
    continuing_from_limit_reached=True causes the first call to use continue_prompt.
    The second call uses the normal static+dynamic prompt.

    Checks:
    - Loop log contains 'interrupted session recovery' for the first call
    - Loop log contains 'continuation prompt' for the second call (normal resumed)
    - Both calls use the same session_id (not new)
    - Two records written to runs_integration.json
    """

    def test_continue_from_limit(self):
        # First get a valid session to "resume from"
        init_log  = str(_INT_DIR / "t4_init_loop.txt")
        init_claude = str(_INT_DIR / "t4_init_claude.txt")

        result_init = self._call(
            primary_prompt        = "You are a one-sentence test assistant.",
            nextLoop_prompt_static = "Reply with a single short sentence only.",
            nextLoop_prompt_dynamic= ["Reply with: T4_INIT"],
            session_id            = None,
            loop_log_file         = init_log,
            claude_log_file       = init_claude,
        )
        self.assertEqual(result_init["success"], 1, "init call must succeed")

        init_runs = self._new_runs()
        self.assertEqual(len(init_runs), 1)
        init_session = init_runs[0]["session_id"]
        self.assertIsNotNone(init_session)
        print(f"\n  init session_id: {init_session}")

        # Now simulate a continuation
        count_before = len(_read_runs())
        recovery_loop_log = str(_INT_DIR / "t4_recovery_loop.txt")

        result = self._call(
            primary_prompt        = "You are a one-sentence test assistant.",
            nextLoop_prompt_static = "Reply with a single short sentence only.",
            nextLoop_prompt_dynamic= [
                "Reply with: T4_RECOVERY",   # loop 0 — use continue_prompt instead
                "Reply with: T4_NORMAL",     # loop 1 — normal prompt resumes
            ],
            session_id                    = init_session,
            continuing_from_limit_reached = True,
            continue_prompt               = "Reply with: T4_CONTINUE_ACK",
            loop_log_file                 = recovery_loop_log,
        )

        self.assertEqual(result["success"],               1)
        self.assertEqual(result["limit_exceeded"],        0)
        self.assertEqual(result["remaining_loop_prompts"], [])

        new = self._new_runs(since=count_before)
        self.assertEqual(len(new), 2, "two loops must have run")

        # Both records should have a session_id (not None)
        for i, rec in enumerate(new):
            self.assertIsNotNone(rec["session_id"], f"loop {i}: session_id is None")

        # Loop log must show the recovery path was taken on first call
        loop_log_content = _log_content(recovery_loop_log)
        self.assertIn(
            "interrupted session recovery", loop_log_content,
            "loop log must record that continue_prompt was used"
        )
        # And the second loop must show normal continuation (not recovery)
        self.assertIn(
            "Prompt: continuation (static + dynamic)", loop_log_content,
            "loop log must show second loop used normal continuation prompt"
        )

        # Extract and verify claude logs
        claude_logs = self._extract_claude_log_paths(recovery_loop_log)
        self.assertEqual(len(claude_logs), 2, "two claude log files must be referenced")

        # Neither call should be a brand-new session
        for i, clog_path in enumerate(claude_logs):
            clog = _log_content(clog_path)
            self.assertNotIn(
                "session=new", clog,
                f"loop {i} must resume existing session, not start new"
            )
            self.assertIn("[CLAUDE]", clog, f"loop {i} log must have Claude output")

        print(f"  recovery loop log: {recovery_loop_log}")
        print(f"  claude logs: {claude_logs}")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print(f"Integration test run: {_RUN_TS}")
    print(f"Logs directory : {_INT_DIR}")
    print(f"Runs file      : {_RUNS_FILE}")
    print("=" * 60)
    unittest.main(verbosity=2)
