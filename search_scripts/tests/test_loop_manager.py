#!/usr/bin/env python3
"""
tests/test_loop_manager.py — Comprehensive tests for search_scripts/loop_manager.py

All four external dependencies are mocked so no real Claude calls, usage queries,
or job-count reads happen.  Temporary directories isolate runs.json and log files.

Run with:
    python -m pytest search_scripts/tests/test_loop_manager.py -v
  or
    python search_scripts/tests/test_loop_manager.py

Test sections:
    A.  Empty dynamic prompt list
    B.  New session prompt building
    C.  Resumed session prompt building
    D.  Session usage threshold
    E.  Weekly usage threshold
    F.  Context length threshold
    G.  Continuing from an interrupted session
    H.  Claude returns STATUS_LIMITS (unexpected API limit)
    I.  Claude returns STATUS_ERROR
    J.  Multiple dynamic prompts
    K.  Rescheduling
    L.  runs.json record integrity
    M.  Usage snapshot unavailable
    N.  Log file creation
    O.  Return structure completeness
    P.  CLI smoke tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Project root on sys.path so imports resolve regardless of how the file is run.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from search_scripts.loop_manager import run_loop
from search_scripts.claude_call import STATUS_OK, STATUS_LIMITS, STATUS_ERROR


# ── shared fixtures ───────────────────────────────────────────────────────────

GOOD_SNAPSHOT = {
    "ok":              True,
    "session_pct":     20.0,
    "weekly_pct":      15.0,
    "session_cost":    1.23,
    "weekly_cost":     8.45,
    "session_end_utc": "2026-05-23T18:00:00+00:00",
    "time_to_reset":   7200.0,
}

SESSION_HIGH_SNAPSHOT = {**GOOD_SNAPSHOT, "session_pct": 95.0}
WEEKLY_HIGH_SNAPSHOT  = {**GOOD_SNAPSHOT, "weekly_pct":  92.0}
BAD_SNAPSHOT          = {"ok": False, "error": "monitor not available"}

LOW_CTX  = {"pct":  10.0, "context":  20_000}
HIGH_CTX = {"pct":  95.0, "context": 190_000}

JOB_COUNT  = 50
SID_A      = "session-aaa"
SID_B      = "session-bbb"
SID_C      = "session-ccc"

_SCRIPT = str(_PROJECT_ROOT / "search_scripts" / "loop_manager.py")


# ── base test class ───────────────────────────────────────────────────────────

class _Base(unittest.TestCase):
    """
    Starts all patches in setUp so every test gets a clean, fully-mocked
    environment.  Each test can override mock return values as needed.
    """

    def setUp(self):
        self.tmp      = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.runs_path = self.tmp_path / "runs.json"
        self.logs_dir  = self.tmp_path / "logs"
        self.logs_dir.mkdir()
        self.runs_path.write_text(json.dumps({"runs": []}))

        self._patches = [
            patch("search_scripts.loop_manager._RUNS_PATH", self.runs_path),
            patch("search_scripts.loop_manager._LOGS_DIR",  self.logs_dir),
            patch("search_scripts.loop_manager.run_claude"),
            patch("search_scripts.loop_manager.get_usage_snapshot"),
            patch("search_scripts.loop_manager.get_context_usage"),
            patch("search_scripts.loop_manager.get_job_count"),
        ]
        (
            _,
            _,
            self.mock_claude,
            self.mock_snapshot,
            self.mock_context,
            self.mock_jobs,
        ) = [p.start() for p in self._patches]

        self.mock_claude.return_value   = (STATUS_OK, SID_A, 5_000)
        self.mock_snapshot.return_value = GOOD_SNAPSHOT
        self.mock_context.return_value  = LOW_CTX
        self.mock_jobs.return_value     = JOB_COUNT

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    # helpers

    def _run(self, **kwargs) -> dict:
        """Call run_loop with minimal defaults; override any kwarg."""
        defaults = {
            "primary_prompt":         "PRIMARY",
            "nextLoop_prompt_static":  "STATIC",
            "nextLoop_prompt_dynamic": ["DYN1"],
        }
        defaults.update(kwargs)
        return run_loop(**defaults)

    def _runs(self) -> list:
        with open(self.runs_path) as f:
            return json.load(f)["runs"]

    def _prompt(self, n: int = 0) -> str:
        """Prompt string passed to the nth run_claude call."""
        return self.mock_claude.call_args_list[n].args[0]

    def _call_args(self, n: int = 0) -> tuple:
        """(prompt, log_path, session_id) for the nth run_claude call."""
        a = self.mock_claude.call_args_list[n].args
        return a[0], a[1], a[2]


# ── A. empty dynamic prompt list ─────────────────────────────────────────────

class TestEmptyDynamicList(_Base):

    def test_no_claude_calls_made(self):
        self._run(nextLoop_prompt_dynamic=[])
        self.mock_claude.assert_not_called()

    def test_returns_success_1_limit_0(self):
        r = self._run(nextLoop_prompt_dynamic=[])
        self.assertEqual(r["success"], 1)
        self.assertEqual(r["limit_exceeded"], 0)

    def test_remaining_prompts_is_empty(self):
        r = self._run(nextLoop_prompt_dynamic=[])
        self.assertEqual(r["remaining_loop_prompts"], [])

    def test_no_runs_record_written(self):
        self._run(nextLoop_prompt_dynamic=[])
        self.assertEqual(self._runs(), [])


# ── B. new session prompt building ───────────────────────────────────────────

class TestNewSessionPrompt(_Base):

    def test_prompt_contains_all_three_parts(self):
        self._run(session_id=None)
        p = self._prompt()
        self.assertIn("PRIMARY", p)
        self.assertIn("STATIC",  p)
        self.assertIn("DYN1",    p)

    def test_claude_called_with_no_session_id(self):
        self._run(session_id=None)
        _, _, sid = self._call_args()
        self.assertIsNone(sid)

    def test_context_not_checked_when_no_session_id(self):
        self._run(session_id=None)
        self.mock_context.assert_not_called()


# ── C. resumed session prompt building ───────────────────────────────────────

class TestResumedSessionPrompt(_Base):

    def test_prompt_omits_primary(self):
        self._run(session_id="existing")
        self.assertNotIn("PRIMARY", self._prompt())

    def test_prompt_contains_static_and_dynamic(self):
        self._run(session_id="existing")
        p = self._prompt()
        self.assertIn("STATIC", p)
        self.assertIn("DYN1",   p)

    def test_claude_called_with_existing_session_id(self):
        self._run(session_id="existing")
        _, _, sid = self._call_args()
        self.assertEqual(sid, "existing")

    def test_context_checked_with_existing_session_id(self):
        self._run(session_id="existing")
        self.mock_context.assert_called_once_with("existing")


# ── D. session usage threshold ────────────────────────────────────────────────

class TestSessionThreshold(_Base):

    def test_stops_before_claude_call(self):
        self.mock_snapshot.return_value = SESSION_HIGH_SNAPSHOT
        self._run(session_threshold=80.0)
        self.mock_claude.assert_not_called()

    def test_returns_limit_exceeded_1(self):
        self.mock_snapshot.return_value = SESSION_HIGH_SNAPSHOT
        r = self._run(session_threshold=80.0)
        self.assertEqual(r["limit_exceeded"], 1)

    def test_success_still_1(self):
        # hitting a threshold is a graceful stop, not a code error
        self.mock_snapshot.return_value = SESSION_HIGH_SNAPSHOT
        r = self._run(session_threshold=80.0)
        self.assertEqual(r["success"], 1)

    def test_remaining_includes_current_prompt(self):
        self.mock_snapshot.return_value = SESSION_HIGH_SNAPSHOT
        r = self._run(nextLoop_prompt_dynamic=["A", "B", "C"], session_threshold=80.0)
        self.assertEqual(r["remaining_loop_prompts"], ["A", "B", "C"])

    def test_exact_boundary_triggers(self):
        snap = {**GOOD_SNAPSHOT, "session_pct": 80.0}
        self.mock_snapshot.return_value = snap
        r = self._run(session_threshold=80.0)
        self.assertEqual(r["limit_exceeded"], 1)

    def test_mid_loop_stop_leaves_correct_remaining(self):
        # Loop 0: pre-check (good) + post-call (good). Loop 1: pre-check (high) → stop.
        self.mock_snapshot.side_effect = [GOOD_SNAPSHOT, GOOD_SNAPSHOT, SESSION_HIGH_SNAPSHOT]
        r = self._run(nextLoop_prompt_dynamic=["A", "B", "C"], session_threshold=80.0)
        self.assertEqual(r["limit_exceeded"], 1)
        self.mock_claude.assert_called_once()
        self.assertEqual(r["remaining_loop_prompts"], ["B", "C"])

    def test_no_runs_record_on_first_loop_stop(self):
        self.mock_snapshot.return_value = SESSION_HIGH_SNAPSHOT
        self._run(session_threshold=80.0)
        self.assertEqual(self._runs(), [])


# ── E. weekly usage threshold ─────────────────────────────────────────────────

class TestWeeklyThreshold(_Base):

    def test_stops_before_claude_call(self):
        self.mock_snapshot.return_value = WEEKLY_HIGH_SNAPSHOT
        self._run(weekly_threshold=80.0)
        self.mock_claude.assert_not_called()

    def test_returns_limit_exceeded_2(self):
        self.mock_snapshot.return_value = WEEKLY_HIGH_SNAPSHOT
        r = self._run(weekly_threshold=80.0)
        self.assertEqual(r["limit_exceeded"], 2)

    def test_remaining_includes_current_prompt(self):
        self.mock_snapshot.return_value = WEEKLY_HIGH_SNAPSHOT
        r = self._run(nextLoop_prompt_dynamic=["A", "B"], weekly_threshold=80.0)
        self.assertEqual(r["remaining_loop_prompts"], ["A", "B"])

    def test_exact_boundary_triggers(self):
        snap = {**GOOD_SNAPSHOT, "weekly_pct": 80.0}
        self.mock_snapshot.return_value = snap
        r = self._run(weekly_threshold=80.0)
        self.assertEqual(r["limit_exceeded"], 2)

    def test_mid_loop_stop(self):
        # Loop 0: pre-check (good) + post-call (good). Loop 1: pre-check (high) → stop.
        self.mock_snapshot.side_effect = [GOOD_SNAPSHOT, GOOD_SNAPSHOT, WEEKLY_HIGH_SNAPSHOT]
        r = self._run(nextLoop_prompt_dynamic=["A", "B"], weekly_threshold=80.0)
        self.assertEqual(r["limit_exceeded"], 2)
        self.mock_claude.assert_called_once()
        self.assertEqual(r["remaining_loop_prompts"], ["B"])

    def test_session_check_still_runs_before_weekly(self):
        # If session_pct is also high, session limit should win (checked first)
        snap = {**GOOD_SNAPSHOT, "session_pct": 95.0, "weekly_pct": 95.0}
        self.mock_snapshot.return_value = snap
        r = self._run(session_threshold=80.0, weekly_threshold=80.0)
        self.assertEqual(r["limit_exceeded"], 1)  # session, not weekly


# ── F. context length threshold ───────────────────────────────────────────────

class TestContextThreshold(_Base):

    def test_high_context_resets_session_id_to_none(self):
        self.mock_context.return_value = HIGH_CTX
        self._run(session_id="old-session", context_threshold=90.0)
        _, _, sid = self._call_args()
        self.assertIsNone(sid)

    def test_high_context_produces_full_prompt(self):
        self.mock_context.return_value = HIGH_CTX
        self._run(session_id="old-session", context_threshold=90.0)
        p = self._prompt()
        self.assertIn("PRIMARY", p)
        self.assertIn("STATIC",  p)
        self.assertIn("DYN1",    p)

    def test_context_exact_boundary_triggers(self):
        self.mock_context.return_value = {"pct": 90.0, "context": 180_000}
        self._run(session_id="s", context_threshold=90.0)
        _, _, sid = self._call_args()
        self.assertIsNone(sid)

    def test_low_context_keeps_session_id(self):
        self.mock_context.return_value = LOW_CTX
        self._run(session_id="keep-me", context_threshold=90.0)
        _, _, sid = self._call_args()
        self.assertEqual(sid, "keep-me")

    def test_context_not_checked_without_session(self):
        self._run(session_id=None, context_threshold=50.0)
        self.mock_context.assert_not_called()

    def test_context_reset_mid_loop_subsequent_loops_use_new_session(self):
        """
        3-loop scenario:
          loop 0 (initial session) — context LOW  → resumed call → returns SID_A
          loop 1 (SID_A)          — context HIGH → context reset → full call, no session → returns SID_B
          loop 2 (SID_B)          — context LOW  → resumed call with SID_B
        """
        self.mock_context.side_effect = [LOW_CTX, HIGH_CTX, LOW_CTX]
        self.mock_claude.side_effect = [
            (STATUS_OK, SID_A, 5_000),
            (STATUS_OK, SID_B, 5_000),
            (STATUS_OK, SID_B, 5_000),
        ]
        self._run(
            session_id="initial",
            nextLoop_prompt_dynamic=["D1", "D2", "D3"],
            context_threshold=90.0,
        )
        # loop 0: resumed, no PRIMARY
        p0, _, sid0 = self._call_args(0)
        self.assertNotIn("PRIMARY", p0)
        self.assertEqual(sid0, "initial")

        # loop 1: context reset → full prompt, no session_id
        p1, _, sid1 = self._call_args(1)
        self.assertIn("PRIMARY", p1)
        self.assertIsNone(sid1)

        # loop 2: new session from loop 1 → resumed, no PRIMARY
        p2, _, sid2 = self._call_args(2)
        self.assertNotIn("PRIMARY", p2)
        self.assertEqual(sid2, SID_B)


# ── G. continuing from an interrupted session ─────────────────────────────────

class TestContinuingFromLimit(_Base):

    def test_uses_continue_prompt_on_first_call(self):
        self._run(
            session_id="resume-me",
            continuing_from_limit_reached=True,
            continue_prompt="CONTINUE THIS",
        )
        p, _, sid = self._call_args()
        self.assertEqual(p, "CONTINUE THIS")
        self.assertEqual(sid, "resume-me")

    def test_second_loop_uses_normal_prompt_not_continue(self):
        self.mock_claude.side_effect = [
            (STATUS_OK, "resume-me", 5_000),
            (STATUS_OK, "resume-me", 5_000),
        ]
        self._run(
            session_id="resume-me",
            continuing_from_limit_reached=True,
            continue_prompt="CONTINUE",
            nextLoop_prompt_dynamic=["DYN1", "DYN2"],
        )
        p1, _, _ = self._call_args(1)
        self.assertNotIn("CONTINUE", p1)
        self.assertIn("STATIC",     p1)
        self.assertIn("DYN2",       p1)

    def test_continue_prompt_only_applied_to_first_call(self):
        self.mock_claude.side_effect = [
            (STATUS_OK, SID_A, 5_000),
            (STATUS_OK, SID_A, 5_000),
            (STATUS_OK, SID_A, 5_000),
        ]
        self._run(
            session_id=SID_A,
            continuing_from_limit_reached=True,
            continue_prompt="CONT",
            nextLoop_prompt_dynamic=["D1", "D2", "D3"],
        )
        # Only the first call should have used CONT
        self.assertIn("CONT", self._call_args(0)[0])
        self.assertNotIn("CONT", self._call_args(1)[0])
        self.assertNotIn("CONT", self._call_args(2)[0])

    def test_missing_continue_prompt_falls_back_to_standard(self):
        # No continue_prompt → fall back to static + dynamic (not primary, since session_id set)
        self._run(
            session_id="resume-me",
            continuing_from_limit_reached=True,
            continue_prompt=None,
        )
        p, _, sid = self._call_args()
        self.assertNotIn("PRIMARY", p)
        self.assertIn("STATIC",     p)
        self.assertIn("DYN1",       p)
        self.assertEqual(sid, "resume-me")


# ── H. Claude returns STATUS_LIMITS ──────────────────────────────────────────

class TestClaudeApiLimit(_Base):

    def test_limit_exceeded_is_3(self):
        self.mock_claude.return_value = (STATUS_LIMITS, SID_A, 5_000)
        r = self._run()
        self.assertEqual(r["limit_exceeded"], 3)

    def test_success_remains_1(self):
        self.mock_claude.return_value = (STATUS_LIMITS, SID_A, 5_000)
        r = self._run()
        self.assertEqual(r["success"], 1)

    def test_remaining_excludes_completed_loop(self):
        self.mock_claude.return_value = (STATUS_LIMITS, SID_A, 5_000)
        r = self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        # loop 0 ran (and hit limit), so B and C remain
        self.assertEqual(r["remaining_loop_prompts"], ["B", "C"])

    def test_stops_after_first_limit(self):
        self.mock_claude.return_value = (STATUS_LIMITS, SID_A, 5_000)
        self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        self.mock_claude.assert_called_once()

    def test_mid_loop_limit_leaves_correct_remaining(self):
        self.mock_claude.side_effect = [
            (STATUS_OK,     SID_A, 5_000),
            (STATUS_LIMITS, SID_A, 5_000),
        ]
        r = self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        self.assertEqual(r["limit_exceeded"], 3)
        self.assertEqual(r["remaining_loop_prompts"], ["C"])

    def test_runs_record_written_with_limit_reached_status(self):
        self.mock_claude.return_value = (STATUS_LIMITS, SID_A, 5_000)
        self._run()
        self.assertEqual(self._runs()[0]["status"], "limit_reached")


# ── I. Claude returns STATUS_ERROR ────────────────────────────────────────────

class TestClaudeError(_Base):

    def test_success_is_0(self):
        self.mock_claude.return_value = (STATUS_ERROR, None, 0)
        r = self._run()
        self.assertEqual(r["success"], 0)

    def test_limit_exceeded_stays_0(self):
        self.mock_claude.return_value = (STATUS_ERROR, None, 0)
        r = self._run()
        self.assertEqual(r["limit_exceeded"], 0)

    def test_remaining_excludes_failed_loop(self):
        self.mock_claude.return_value = (STATUS_ERROR, None, 0)
        r = self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        self.assertEqual(r["remaining_loop_prompts"], ["B", "C"])

    def test_stops_after_error(self):
        self.mock_claude.return_value = (STATUS_ERROR, None, 0)
        self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        self.mock_claude.assert_called_once()

    def test_runs_record_written_with_error_status(self):
        self.mock_claude.return_value = (STATUS_ERROR, None, 0)
        self._run()
        self.assertEqual(self._runs()[0]["status"], "error")

    def test_mid_loop_error_leaves_correct_remaining(self):
        self.mock_claude.side_effect = [
            (STATUS_OK,    SID_A, 5_000),
            (STATUS_ERROR, None,  0),
        ]
        r = self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        self.assertEqual(r["success"], 0)
        self.assertEqual(r["remaining_loop_prompts"], ["C"])


# ── J. multiple dynamic prompts ───────────────────────────────────────────────

class TestMultipleDynamicPrompts(_Base):

    def test_all_three_loops_complete(self):
        r = self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        self.assertEqual(r["success"], 1)
        self.assertEqual(r["limit_exceeded"], 0)
        self.assertEqual(r["remaining_loop_prompts"], [])
        self.assertEqual(self.mock_claude.call_count, 3)

    def test_each_loop_includes_its_own_dynamic_part(self):
        self._run(nextLoop_prompt_dynamic=["ALPHA", "BETA", "GAMMA"])
        self.assertIn("ALPHA", self._prompt(0))
        self.assertIn("BETA",  self._prompt(1))
        self.assertIn("GAMMA", self._prompt(2))

    def test_only_first_loop_includes_primary_for_new_session(self):
        self._run(session_id=None, nextLoop_prompt_dynamic=["A", "B", "C"])
        self.assertIn("PRIMARY",    self._prompt(0))
        self.assertNotIn("PRIMARY", self._prompt(1))
        self.assertNotIn("PRIMARY", self._prompt(2))

    def test_no_loop_includes_primary_for_resumed_session(self):
        self.mock_claude.side_effect = [
            (STATUS_OK, SID_A, 5_000),
            (STATUS_OK, SID_A, 5_000),
            (STATUS_OK, SID_A, 5_000),
        ]
        self._run(session_id=SID_A, nextLoop_prompt_dynamic=["A", "B", "C"])
        for i in range(3):
            self.assertNotIn("PRIMARY", self._prompt(i))

    def test_session_id_propagates_across_loops(self):
        self.mock_claude.side_effect = [
            (STATUS_OK, SID_A, 5_000),
            (STATUS_OK, SID_B, 5_000),
            (STATUS_OK, SID_C, 5_000),
        ]
        self._run(session_id=None, nextLoop_prompt_dynamic=["A", "B", "C"])
        _, _, sid0 = self._call_args(0)
        _, _, sid1 = self._call_args(1)
        _, _, sid2 = self._call_args(2)
        self.assertIsNone(sid0)       # first call: no session yet
        self.assertEqual(sid1, SID_A) # second uses result of first
        self.assertEqual(sid2, SID_B) # third uses result of second

    def test_all_loops_include_static_part(self):
        self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        for i in range(3):
            self.assertIn("STATIC", self._prompt(i))


# ── K. rescheduling ───────────────────────────────────────────────────────────

class TestRescheduling(_Base):

    def test_session_limit_reschedule_time_set(self):
        self.mock_snapshot.return_value = SESSION_HIGH_SNAPSHOT
        r = self._run(session_threshold=80.0, allow_reschedule=True)
        self.assertEqual(r["reschedule_time"], GOOD_SNAPSHOT["session_end_utc"])

    def test_session_limit_no_reschedule_when_not_allowed(self):
        self.mock_snapshot.return_value = SESSION_HIGH_SNAPSHOT
        r = self._run(session_threshold=80.0, allow_reschedule=False)
        self.assertIsNone(r["reschedule_time"])

    def test_weekly_limit_reschedule_time_is_none(self):
        # Weekly limit has no defined per-block reset time
        self.mock_snapshot.return_value = WEEKLY_HIGH_SNAPSHOT
        r = self._run(weekly_threshold=80.0, allow_reschedule=True)
        self.assertIsNone(r["reschedule_time"])

    def test_api_limit_reschedule_time_set(self):
        self.mock_claude.return_value = (STATUS_LIMITS, SID_A, 5_000)
        # pre-check snapshot, post-call snapshot for record, fresh snapshot for reschedule
        self.mock_snapshot.side_effect = [GOOD_SNAPSHOT, GOOD_SNAPSHOT, GOOD_SNAPSHOT]
        r = self._run(allow_reschedule=True)
        self.assertEqual(r["reschedule_time"], GOOD_SNAPSHOT["session_end_utc"])

    def test_api_limit_no_reschedule_when_not_allowed(self):
        self.mock_claude.return_value = (STATUS_LIMITS, SID_A, 5_000)
        r = self._run(allow_reschedule=False)
        self.assertIsNone(r["reschedule_time"])

    def test_clean_completion_reschedule_time_none(self):
        r = self._run(allow_reschedule=True)
        self.assertIsNone(r["reschedule_time"])


# ── L. runs.json record integrity ─────────────────────────────────────────────

class TestRunsJsonRecords(_Base):

    def test_one_loop_writes_one_record(self):
        self._run()
        self.assertEqual(len(self._runs()), 1)

    def test_three_loops_write_three_records(self):
        self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        self.assertEqual(len(self._runs()), 3)

    def test_record_has_all_required_fields(self):
        self._run()
        rec = self._runs()[0]
        for field in (
            "started", "ended", "status", "loop_index",
            "session_id", "session_cost", "weekly_cost",
            "jobs_before", "jobs_after", "jobs_new", "errors",
        ):
            self.assertIn(field, rec, msg=f"Missing field: {field}")

    def test_record_loop_index_correct(self):
        self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        records = self._runs()
        self.assertEqual(records[0]["loop_index"], 0)
        self.assertEqual(records[1]["loop_index"], 1)
        self.assertEqual(records[2]["loop_index"], 2)

    def test_record_status_success(self):
        self._run()
        self.assertEqual(self._runs()[0]["status"], "success")

    def test_record_job_count_delta(self):
        self.mock_jobs.side_effect = [50, 53]
        self._run()
        rec = self._runs()[0]
        self.assertEqual(rec["jobs_before"], 50)
        self.assertEqual(rec["jobs_after"],  53)
        self.assertEqual(rec["jobs_new"],     3)

    def test_record_job_count_delta_multiple_loops(self):
        # jobs: 50 → 53 (loop 0), 53 → 55 (loop 1)
        self.mock_jobs.side_effect = [50, 53, 53, 55]
        self._run(nextLoop_prompt_dynamic=["A", "B"])
        records = self._runs()
        self.assertEqual(records[0]["jobs_new"], 3)
        self.assertEqual(records[1]["jobs_new"], 2)

    def test_record_session_id_from_claude_return(self):
        self._run(session_id=None)
        self.assertEqual(self._runs()[0]["session_id"], SID_A)

    def test_appends_to_existing_records(self):
        existing = {"runs": [{"status": "old", "loop_index": -1}]}
        self.runs_path.write_text(json.dumps(existing))
        self._run()
        records = self._runs()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["loop_index"], -1)  # original preserved

    def test_created_fresh_when_missing(self):
        self.runs_path.unlink()
        self._run()
        self.assertTrue(self.runs_path.exists())
        self.assertEqual(len(self._runs()), 1)

    def test_no_record_on_threshold_stop_before_call(self):
        self.mock_snapshot.return_value = SESSION_HIGH_SNAPSHOT
        self._run(session_threshold=80.0)
        self.assertEqual(self._runs(), [])

    def test_cost_fields_come_from_post_call_snapshot(self):
        # Pre-call snapshot has cost 1.23 / 8.45; post-call has 2.50 / 9.00
        post_snap = {**GOOD_SNAPSHOT, "session_cost": 2.50, "weekly_cost": 9.00}
        self.mock_snapshot.side_effect = [GOOD_SNAPSHOT, post_snap]
        self._run()
        rec = self._runs()[0]
        self.assertAlmostEqual(rec["session_cost"], 2.50)
        self.assertAlmostEqual(rec["weekly_cost"],  9.00)


# ── M. usage snapshot unavailable ────────────────────────────────────────────

class TestSnapshotUnavailable(_Base):

    def test_loop_continues_when_snapshot_bad(self):
        self.mock_snapshot.return_value = BAD_SNAPSHOT
        r = self._run()
        self.assertEqual(r["success"], 1)
        self.mock_claude.assert_called_once()

    def test_no_threshold_stop_when_snapshot_bad(self):
        # Even with aggressive thresholds, a bad snapshot should not stop the loop
        self.mock_snapshot.return_value = BAD_SNAPSHOT
        r = self._run(session_threshold=0.0, weekly_threshold=0.0)
        self.assertEqual(r["limit_exceeded"], 0)
        self.mock_claude.assert_called_once()

    def test_multiple_loops_all_run_when_snapshot_always_bad(self):
        self.mock_snapshot.return_value = BAD_SNAPSHOT
        r = self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        self.assertEqual(self.mock_claude.call_count, 3)
        self.assertEqual(r["success"], 1)


# ── N. log file creation ──────────────────────────────────────────────────────

class TestLogFiles(_Base):

    def test_loop_log_created_in_logs_dir(self):
        self._run()
        logs = list(self.logs_dir.glob("loop_log_*.txt"))
        self.assertEqual(len(logs), 1)

    def test_custom_loop_log_path_used(self):
        custom = str(self.tmp_path / "my_loop.log")
        self._run(loop_log_file=custom)
        self.assertTrue(Path(custom).exists())

    def test_default_claude_log_differs_per_loop(self):
        self._run(nextLoop_prompt_dynamic=["A", "B"])
        log0 = self.mock_claude.call_args_list[0].args[1]
        log1 = self.mock_claude.call_args_list[1].args[1]
        self.assertNotEqual(log0, log1)

    def test_custom_claude_log_same_for_all_loops(self):
        fixed = str(self.tmp_path / "fixed_claude.log")
        self._run(nextLoop_prompt_dynamic=["A", "B"], claude_log_file=fixed)
        log0 = self.mock_claude.call_args_list[0].args[1]
        log1 = self.mock_claude.call_args_list[1].args[1]
        self.assertEqual(log0, fixed)
        self.assertEqual(log1, fixed)


# ── O. return structure completeness ─────────────────────────────────────────

class TestReturnStructure(_Base):

    def test_all_keys_present_on_success(self):
        r = self._run()
        for key in ("success", "limit_exceeded", "remaining_loop_prompts", "reschedule_time"):
            self.assertIn(key, r, msg=f"Missing key: {key}")

    def test_all_keys_present_on_limit(self):
        self.mock_snapshot.return_value = SESSION_HIGH_SNAPSHOT
        r = self._run(session_threshold=80.0)
        for key in ("success", "limit_exceeded", "remaining_loop_prompts", "reschedule_time"):
            self.assertIn(key, r)

    def test_all_keys_present_on_error(self):
        self.mock_claude.return_value = (STATUS_ERROR, None, 0)
        r = self._run()
        for key in ("success", "limit_exceeded", "remaining_loop_prompts", "reschedule_time"):
            self.assertIn(key, r)

    def test_remaining_prompts_is_list(self):
        r = self._run()
        self.assertIsInstance(r["remaining_loop_prompts"], list)

    def test_full_completion_remaining_is_empty(self):
        r = self._run(nextLoop_prompt_dynamic=["A", "B", "C"])
        self.assertEqual(r["remaining_loop_prompts"], [])

    def test_success_values_are_int(self):
        r = self._run()
        self.assertIsInstance(r["success"],        int)
        self.assertIsInstance(r["limit_exceeded"], int)


# ── P. CLI smoke tests ────────────────────────────────────────────────────────

class TestCLI(unittest.TestCase):

    def _run_cli(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, _SCRIPT] + args,
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
        )

    def test_help_exits_zero(self):
        r = self._run_cli(["--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("--primary-prompt", r.stdout)

    def test_missing_required_args_exits_nonzero(self):
        r = self._run_cli([])
        self.assertNotEqual(r.returncode, 0)

    def test_missing_dynamic_prompts_exits_nonzero(self):
        r = self._run_cli([
            "--primary-prompt", "P",
            "--static-prompt",  "S",
            # no --dynamic-prompts
        ])
        self.assertNotEqual(r.returncode, 0)

    def test_output_ends_with_valid_json(self):
        # The CLI prints loop-log lines AND then a JSON result to stdout.
        # Extract the trailing JSON block (everything from the last bare '{').
        r = self._run_cli([
            "--primary-prompt",    "P",
            "--static-prompt",     "S",
            "--dynamic-prompts",   "D1",
            "--session-threshold", "0",
        ])
        # Find the last line that is exactly '{' — start of the JSON dict.
        lines = r.stdout.splitlines()
        json_start = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == "{":
                json_start = i
                break
        if json_start is None:
            self.fail(f"No JSON block found in CLI stdout:\n{r.stdout!r}")
        try:
            data = json.loads("\n".join(lines[json_start:]))
        except json.JSONDecodeError as exc:
            self.fail(f"JSON parse error: {exc}\nOutput:\n{r.stdout!r}")
        for key in ("success", "limit_exceeded", "remaining_loop_prompts", "reschedule_time"):
            self.assertIn(key, data, msg=f"Missing key: {key}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
