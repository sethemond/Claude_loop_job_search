#!/usr/bin/env python3
"""
test_schedule_manager.py — Comprehensive tests for search_scripts/schedule_manager.py

All file I/O is redirected to temporary directories.  run_loop is always mocked
so no real Claude calls are made.

Run:
    python search_scripts/tests/test_schedule_manager.py
  or
    python -m pytest search_scripts/tests/test_schedule_manager.py -v

Test sections:
    A.  _compute_outcome — pure function (all branches)
    B.  File I/O helpers — _load_schedules, _save_schedules, _read_last_session_id,
                           _write_loop_state, _parse_iso
    C.  schedule_loop — entry creation and scheduling logic
    D.  run_schedule_now — fire and edge cases
    E.  cancel_schedule — idle and running-flag paths
    F.  get_schedules — annotation and _is_running flag
    G.  _fire_due — timing, filtering, double-fire prevention
    H.  _execute — status transitions with mocked run_loop
    I.  Singleton — get_manager thread-safety
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import search_scripts.schedule_manager as sm
from search_scripts.schedule_manager import (
    _compute_outcome,
    _load_schedules,
    _save_schedules,
    _read_last_session_id,
    _write_loop_state,
    _parse_iso,
    _iso,
    _utcnow,
    ScheduleManager,
    get_manager,
)


# ── shared fixtures ───────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 23, 14, 30, 0, tzinfo=timezone.utc)
_TOMORROW_SAME = datetime(2026, 5, 24, 14, 30, 0, tzinfo=timezone.utc)

_GOOD_RESULT = {
    "success": 1,
    "limit_exceeded": 0,
    "remaining_loop_prompts": [],
    "reschedule_time": None,
}

_LIMIT1_RESULT = {
    "success": 0,
    "limit_exceeded": 1,
    "remaining_loop_prompts": ["prompt-A", "prompt-B"],
    "reschedule_time": "2026-05-23T16:00:00+00:00",
}

_ENTRY_BASE = {
    "id": "test-sched-id",
    "hour_utc": 14,
    "minute_utc": 30,
    "repeat": 0,
    "runs_remaining": 0,
    "allow_reschedule": True,
    "status": "active",
    "next_run": _iso(_NOW),
    "last_run": None,
    "session_id": None,
    "remaining_loop_prompts": [],
    "continuing_from_limit_reached": False,
    "settings": {
        "primary_prompt": "PRIMARY",
        "nextLoop_prompt_static": "STATIC",
        "nextLoop_prompt_dynamic": ["DYN1"],
        "session_threshold": 80.0,
        "weekly_threshold": 80.0,
        "context_threshold": 90.0,
    },
    "last_result": None,
}


def _make_entry(**overrides) -> dict:
    e = dict(_ENTRY_BASE)
    e.update(overrides)
    return e


# ── base test class ───────────────────────────────────────────────────────────

class _Base(unittest.TestCase):
    """Redirect all file paths to a temp directory; mock run_loop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.schedules_path  = self.tmp_path / "schedules.json"
        self.loop_state_path = self.tmp_path / "loop_state.json"
        self.runs_path       = self.tmp_path / "runs.json"

        self.schedules_path.write_text(json.dumps({"schedules": []}))
        self.runs_path.write_text(json.dumps({"runs": []}))

        self._patches = [
            patch.object(sm, "_SCHEDULES_PATH",  self.schedules_path),
            patch.object(sm, "_LOOP_STATE_PATH", self.loop_state_path),
            patch.object(sm, "_RUNS_PATH",       self.runs_path),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    # helpers

    def _write_schedules(self, entries: list) -> None:
        self.schedules_path.write_text(json.dumps({"schedules": entries}))

    def _read_schedules(self) -> list:
        return json.loads(self.schedules_path.read_text())["schedules"]

    def _write_runs(self, runs: list) -> None:
        self.runs_path.write_text(json.dumps({"runs": runs}))

    def _read_loop_state(self) -> dict:
        if not self.loop_state_path.exists():
            return {}
        return json.loads(self.loop_state_path.read_text())

    def _make_manager(self, start=False) -> ScheduleManager:
        """Create a fresh ScheduleManager without starting the tick thread."""
        mgr = ScheduleManager()
        if start:
            mgr.start()
        return mgr


# ═══════════════════════════════════════════════════════════════════════════════
# A.  _compute_outcome — pure function
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeOutcome(unittest.TestCase):

    def _call(self, **kwargs):
        defaults = dict(
            limit=0,
            success=1,
            rem=[],
            reschedule_time=None,
            allow_reschedule=True,
            runs_remaining=0,
            entry={"hour_utc": 10, "minute_utc": 0},
        )
        defaults.update(kwargs)
        with patch.object(sm, "_utcnow", return_value=_NOW):
            return _compute_outcome(**defaults)

    # A1 — success=0 always → error
    def test_A1_error_on_success_zero(self):
        status, next_run, rem_count = self._call(success=0, runs_remaining=5)
        self.assertEqual(status, "error")
        self.assertIsNone(next_run)
        self.assertEqual(rem_count, 5)  # unchanged

    # A2 — session limit (1) + reschedule_time + allow=True → active with reschedule
    def test_A2_session_limit_reschedule(self):
        rt = "2026-05-23T16:00:00+00:00"
        status, next_run, rem = self._call(limit=1, reschedule_time=rt, allow_reschedule=True)
        self.assertEqual(status, "active")
        self.assertEqual(next_run, _parse_iso(rt))
        self.assertEqual(rem, 0)  # runs_remaining unchanged

    # A3 — API limit (3) + reschedule_time + allow=True → active
    def test_A3_api_limit_reschedule(self):
        rt = "2026-05-23T17:00:00+00:00"
        status, next_run, _ = self._call(limit=3, reschedule_time=rt, allow_reschedule=True)
        self.assertEqual(status, "active")
        self.assertEqual(next_run, _parse_iso(rt))

    # A4 — session limit (1) + allow=False → limit_reached (no reschedule)
    def test_A4_session_limit_no_allow(self):
        rt = "2026-05-23T16:00:00+00:00"
        status, next_run, _ = self._call(limit=1, reschedule_time=rt, allow_reschedule=False)
        self.assertEqual(status, "limit_reached")
        self.assertIsNone(next_run)

    # A5 — session limit (1) + no reschedule_time → limit_reached
    def test_A5_session_limit_no_reschedule_time(self):
        status, next_run, _ = self._call(limit=1, reschedule_time=None, allow_reschedule=True)
        self.assertEqual(status, "limit_reached")
        self.assertIsNone(next_run)

    # A6 — weekly limit (2) + allow=True → limit_reached (no reschedule_time ever)
    def test_A6_weekly_limit_always_limit_reached(self):
        rt = "2026-05-23T16:00:00+00:00"
        status, next_run, _ = self._call(limit=2, reschedule_time=rt, allow_reschedule=True)
        self.assertEqual(status, "limit_reached")
        self.assertIsNone(next_run)

    # A7 — success + runs_remaining=0 → completed
    def test_A7_success_runs_remaining_zero(self):
        status, next_run, rem = self._call(runs_remaining=0)
        self.assertEqual(status, "completed")
        self.assertIsNone(next_run)
        self.assertEqual(rem, 0)

    # A8 — success + runs_remaining=-1 → active, next_day_same_hour, remains -1
    def test_A8_success_forever(self):
        status, next_run, rem = self._call(
            runs_remaining=-1,
            entry={"hour_utc": 14, "minute_utc": 30},
        )
        self.assertEqual(status, "active")
        self.assertIsNotNone(next_run)
        self.assertEqual(rem, -1)
        # next_run must be tomorrow at 14:30 UTC (candidate at 14:30 == _NOW, so +1 day)
        expected = _NOW.replace(hour=14, minute=30, second=0, microsecond=0) + timedelta(days=1)
        self.assertEqual(next_run, expected)

    # A9 — success + runs_remaining=3 → active, decremented to 2
    def test_A9_success_decrement_runs(self):
        status, next_run, rem = self._call(
            runs_remaining=3,
            entry={"hour_utc": 14, "minute_utc": 30},
        )
        self.assertEqual(status, "active")
        self.assertIsNotNone(next_run)
        self.assertEqual(rem, 2)

    # A10 — success + runs_remaining=1 → active (next run will complete), rem=0
    def test_A10_success_penultimate_run(self):
        status, next_run, rem = self._call(
            runs_remaining=1,
            entry={"hour_utc": 14, "minute_utc": 30},
        )
        self.assertEqual(status, "active")
        self.assertIsNotNone(next_run)
        self.assertEqual(rem, 0)

    # A11 — next_run timing: hour in future today → schedules today not tomorrow
    def test_A11_next_run_today_when_hour_future(self):
        # _NOW is 14:30; schedule hour=15 → candidate 15:00 today is in the future
        status, next_run, _ = self._call(
            runs_remaining=-1,
            entry={"hour_utc": 15, "minute_utc": 0},
        )
        self.assertEqual(status, "active")
        expected_date = _NOW.date()
        self.assertEqual(next_run.date(), expected_date)
        self.assertEqual(next_run.hour, 15)
        self.assertEqual(next_run.minute, 0)

    # A12 — minute_utc respected
    def test_A12_minute_utc_respected(self):
        status, next_run, _ = self._call(
            runs_remaining=-1,
            entry={"hour_utc": 14, "minute_utc": 45},
        )
        self.assertEqual(next_run.minute, 45)


# ═══════════════════════════════════════════════════════════════════════════════
# B.  File I/O helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestHelpers(_Base):

    # B1 — _parse_iso with timezone
    def test_B1_parse_iso_with_tz(self):
        s = "2026-05-23T14:30:00+00:00"
        dt = _parse_iso(s)
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.hour, 14)

    # B2 — _parse_iso without timezone → UTC assumed
    def test_B2_parse_iso_no_tz(self):
        s = "2026-05-23T14:30:00"
        dt = _parse_iso(s)
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(dt.hour, 14)

    # B3 — _load_schedules missing file → empty
    def test_B3_load_schedules_missing_file(self):
        self.schedules_path.unlink()
        data = _load_schedules()
        self.assertEqual(data, {"schedules": []})

    # B4 — _load_schedules corrupt file → empty
    def test_B4_load_schedules_corrupt(self):
        self.schedules_path.write_text("not json {{{")
        data = _load_schedules()
        self.assertEqual(data, {"schedules": []})

    # B5 — _load_schedules wrong structure → empty
    def test_B5_load_schedules_wrong_structure(self):
        self.schedules_path.write_text(json.dumps([1, 2, 3]))
        data = _load_schedules()
        self.assertEqual(data, {"schedules": []})

    # B6 — _load_schedules / _save_schedules roundtrip
    def test_B6_save_load_roundtrip(self):
        original = {"schedules": [_make_entry()]}
        _save_schedules(original)
        loaded = _load_schedules()
        self.assertEqual(loaded["schedules"][0]["id"], "test-sched-id")

    # B7 — _read_last_session_id missing runs.json → None
    def test_B7_read_session_id_missing_file(self):
        self.runs_path.unlink()
        self.assertIsNone(_read_last_session_id())

    # B8 — _read_last_session_id empty runs list → None
    def test_B8_read_session_id_empty_list(self):
        self.assertIsNone(_read_last_session_id())

    # B9 — _read_last_session_id returns last record's session_id
    def test_B9_read_session_id_returns_last(self):
        self._write_runs([
            {"session_id": "first-sid"},
            {"session_id": "last-sid"},
        ])
        self.assertEqual(_read_last_session_id(), "last-sid")

    # B10 — _read_last_session_id handles corrupt file → None
    def test_B10_read_session_id_corrupt_file(self):
        self.runs_path.write_text("bad json")
        self.assertIsNone(_read_last_session_id())

    # B11 — _write_loop_state creates file if missing
    def test_B11_write_loop_state_creates_file(self):
        _write_loop_state("running", sched_id="abc-123")
        state = self._read_loop_state()
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["active_sched_id"], "abc-123")

    # B12 — _write_loop_state merges with existing state
    def test_B12_write_loop_state_merges(self):
        self.loop_state_path.write_text(json.dumps({"existing_key": "keep_me"}))
        _write_loop_state("idle")
        state = self._read_loop_state()
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state.get("existing_key"), "keep_me")

    # B13 — _write_loop_state includes error when provided
    def test_B13_write_loop_state_error_field(self):
        _write_loop_state("error", error="boom")
        state = self._read_loop_state()
        self.assertEqual(state["error"], "boom")


# ═══════════════════════════════════════════════════════════════════════════════
# C.  schedule_loop — entry creation
# ═══════════════════════════════════════════════════════════════════════════════

class TestScheduleLoop(_Base):

    def setUp(self):
        super().setUp()
        self.mgr = self._make_manager()

    def _schedule(self, **kwargs) -> str:
        defaults = dict(
            primary_prompt="PRIMARY",
            nextLoop_prompt_static="STATIC",
            nextLoop_prompt_dynamic=["DYN1", "DYN2"],
        )
        defaults.update(kwargs)
        return self.mgr.schedule_loop(defaults)

    # C1 — returns a non-empty UUID string
    def test_C1_returns_uuid_string(self):
        sid = self._schedule(now=True)
        self.assertIsInstance(sid, str)
        self.assertTrue(len(sid) > 0)

    # C2 — entry written to schedules.json
    def test_C2_entry_persisted(self):
        sid = self._schedule(now=True)
        entries = self._read_schedules()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], sid)

    # C3 — now=True sets next_run ≈ now
    def test_C3_now_true_fires_immediately(self):
        # _iso() truncates to seconds, so compare at second granularity
        before = _utcnow().replace(microsecond=0)
        sid = self._schedule(now=True)
        after = _utcnow()
        entry = self._read_schedules()[0]
        next_run = _parse_iso(entry["next_run"])
        self.assertGreaterEqual(next_run, before)
        self.assertLessEqual(next_run, after)

    # C4 — now=False, future hour → schedules at correct UTC time today or tomorrow
    def test_C4_timed_schedule_correct_next_run(self):
        with patch.object(sm, "_utcnow", return_value=_NOW):
            # 14:30 now; schedule at 15:00 UTC — still today
            sid = self._schedule(hour_utc=15, minute_utc=0)
        entry = self._read_schedules()[0]
        next_run = _parse_iso(entry["next_run"])
        self.assertEqual(next_run.hour, 15)
        self.assertEqual(next_run.minute, 0)
        # Should be today (same date as _NOW = 2026-05-23)
        self.assertEqual(next_run.date(), _NOW.date())

    # C5 — now=False, past hour → schedules tomorrow
    def test_C5_past_hour_schedules_tomorrow(self):
        with patch.object(sm, "_utcnow", return_value=_NOW):
            # 14:30 now; schedule at 10:00 UTC (already passed) → tomorrow
            sid = self._schedule(hour_utc=10, minute_utc=0)
        entry = self._read_schedules()[0]
        next_run = _parse_iso(entry["next_run"])
        self.assertEqual(next_run.hour, 10)
        tomorrow = (_NOW + timedelta(days=1)).date()
        self.assertEqual(next_run.date(), tomorrow)

    # C6 — repeat and runs_remaining set correctly
    def test_C6_repeat_sets_runs_remaining(self):
        self._schedule(now=True, repeat=3)
        entry = self._read_schedules()[0]
        self.assertEqual(entry["repeat"], 3)
        self.assertEqual(entry["runs_remaining"], 3)

    # C7 — repeat=-1 (forever) persisted
    def test_C7_repeat_forever(self):
        self._schedule(now=True, repeat=-1)
        entry = self._read_schedules()[0]
        self.assertEqual(entry["runs_remaining"], -1)

    # C8 — allow_reschedule stored from config
    def test_C8_allow_reschedule_stored(self):
        self._schedule(now=True, allow_reschedule=True)
        entry = self._read_schedules()[0]
        self.assertTrue(entry["allow_reschedule"])

    # C9 — allow_reschedule defaults to False
    def test_C9_allow_reschedule_default_false(self):
        self._schedule(now=True)
        entry = self._read_schedules()[0]
        self.assertFalse(entry["allow_reschedule"])

    # C10 — settings nested correctly
    def test_C10_settings_nested(self):
        self._schedule(now=True, session_threshold=95.0)
        entry = self._read_schedules()[0]
        self.assertEqual(entry["settings"]["session_threshold"], 95.0)
        self.assertEqual(entry["settings"]["primary_prompt"], "PRIMARY")

    # C11 — wakeup event is set after scheduling
    def test_C11_wakeup_set_after_schedule(self):
        self.mgr._wakeup.clear()
        self._schedule(now=True)
        self.assertTrue(self.mgr._wakeup.is_set())

    # C12 — multiple schedules can coexist
    def test_C12_multiple_schedules_coexist(self):
        id1 = self._schedule(now=True)
        id2 = self._schedule(now=True)
        entries = self._read_schedules()
        self.assertEqual(len(entries), 2)
        ids = {e["id"] for e in entries}
        self.assertIn(id1, ids)
        self.assertIn(id2, ids)


# ═══════════════════════════════════════════════════════════════════════════════
# D.  run_schedule_now
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunScheduleNow(_Base):

    def setUp(self):
        super().setUp()
        self.mgr = self._make_manager()

    # D1 — not found → False
    def test_D1_not_found_returns_false(self):
        self.assertFalse(self.mgr.run_schedule_now("nonexistent-id"))

    # D2 — already running → False
    def test_D2_already_running_returns_false(self):
        entry = _make_entry()
        self._write_schedules([entry])
        self.mgr._running[entry["id"]] = MagicMock()  # simulate running
        self.assertFalse(self.mgr.run_schedule_now(entry["id"]))

    # D3 — idle schedule → submits to executor, returns True
    def test_D3_idle_schedule_submits(self):
        entry = _make_entry(status="active")
        self._write_schedules([entry])
        with patch.object(self.mgr._executor, "submit") as mock_submit:
            mock_submit.return_value = MagicMock()
            result = self.mgr.run_schedule_now(entry["id"])
        self.assertTrue(result)
        mock_submit.assert_called_once()

    # D4 — run_schedule_now updates next_run to now
    def test_D4_updates_next_run_to_now(self):
        future_time = _iso(_utcnow() + timedelta(hours=5))
        entry = _make_entry(status="active", next_run=future_time)
        self._write_schedules([entry])
        # _iso() truncates to seconds; compare at second granularity
        before = _utcnow().replace(microsecond=0)
        with patch.object(self.mgr._executor, "submit") as mock_submit:
            mock_submit.return_value = MagicMock()
            self.mgr.run_schedule_now(entry["id"])
        after = _utcnow()
        updated = self._read_schedules()[0]
        new_next = _parse_iso(updated["next_run"])
        self.assertGreaterEqual(new_next, before)
        self.assertLessEqual(new_next, after)

    # D5 — schedule added to _running after run_schedule_now
    def test_D5_added_to_running(self):
        entry = _make_entry(status="active")
        self._write_schedules([entry])
        with patch.object(self.mgr._executor, "submit") as mock_submit:
            mock_submit.return_value = MagicMock()
            self.mgr.run_schedule_now(entry["id"])
        self.assertIn(entry["id"], self.mgr._running)


# ═══════════════════════════════════════════════════════════════════════════════
# E.  cancel_schedule
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelSchedule(_Base):

    def setUp(self):
        super().setUp()
        self.mgr = self._make_manager()

    # E1 — not found → False
    def test_E1_not_found_returns_false(self):
        self.assertFalse(self.mgr.cancel_schedule("nonexistent-id"))

    # E2 — idle schedule → removed immediately, returns True
    def test_E2_idle_removed_immediately(self):
        entry = _make_entry()
        self._write_schedules([entry])
        result = self.mgr.cancel_schedule(entry["id"])
        self.assertTrue(result)
        self.assertEqual(self._read_schedules(), [])

    # E3 — running schedule → marked in _cancelled, NOT removed yet
    def test_E3_running_schedule_marked_cancelled(self):
        entry = _make_entry()
        self._write_schedules([entry])
        self.mgr._running[entry["id"]] = MagicMock()
        result = self.mgr.cancel_schedule(entry["id"])
        self.assertTrue(result)
        self.assertIn(entry["id"], self.mgr._cancelled)
        # Entry still in schedules.json until worker finishes
        self.assertEqual(len(self._read_schedules()), 1)

    # E4 — wakeup set after cancel
    def test_E4_wakeup_set_after_cancel(self):
        entry = _make_entry()
        self._write_schedules([entry])
        self.mgr._wakeup.clear()
        self.mgr.cancel_schedule(entry["id"])
        self.assertTrue(self.mgr._wakeup.is_set())


# ═══════════════════════════════════════════════════════════════════════════════
# F.  get_schedules — annotation
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetSchedules(_Base):

    def setUp(self):
        super().setUp()
        self.mgr = self._make_manager()

    # F1 — empty → returns empty list
    def test_F1_empty_returns_empty_list(self):
        self.assertEqual(self.mgr.get_schedules(), [])

    # F2 — _is_running=False for idle entry
    def test_F2_is_running_false_for_idle(self):
        self._write_schedules([_make_entry()])
        result = self.mgr.get_schedules()
        self.assertFalse(result[0]["_is_running"])

    # F3 — _is_running=True for entry in _running
    def test_F3_is_running_true_for_running(self):
        entry = _make_entry()
        self._write_schedules([entry])
        self.mgr._running[entry["id"]] = MagicMock()
        result = self.mgr.get_schedules()
        self.assertTrue(result[0]["_is_running"])

    # F4 — _remaining_count reflects len(remaining_loop_prompts)
    def test_F4_remaining_count_correct(self):
        entry = _make_entry(remaining_loop_prompts=["a", "b", "c"])
        self._write_schedules([entry])
        result = self.mgr.get_schedules()
        self.assertEqual(result[0]["_remaining_count"], 3)

    # F5 — _remaining_count=0 when no remaining
    def test_F5_remaining_count_zero_when_empty(self):
        entry = _make_entry(remaining_loop_prompts=[])
        self._write_schedules([entry])
        result = self.mgr.get_schedules()
        self.assertEqual(result[0]["_remaining_count"], 0)

    # F6 — original dict not mutated by annotation
    def test_F6_original_not_mutated(self):
        entry = _make_entry()
        self._write_schedules([entry])
        self.mgr.get_schedules()
        # Original in schedules.json must not have _is_running
        raw = self._read_schedules()[0]
        self.assertNotIn("_is_running", raw)


# ═══════════════════════════════════════════════════════════════════════════════
# G.  _fire_due — timing and filtering
# ═══════════════════════════════════════════════════════════════════════════════

class TestFireDue(_Base):

    def setUp(self):
        super().setUp()
        self.mgr = self._make_manager()

    def _fire(self) -> float:
        with patch.object(self.mgr._executor, "submit") as self.mock_submit:
            self.mock_submit.return_value = MagicMock()
            return self.mgr._fire_due()

    # G1 — no schedules → returns _TICK_CAP
    def test_G1_no_schedules_returns_tick_cap(self):
        result = self._fire()
        self.assertEqual(result, sm._TICK_CAP)

    # G2 — active schedule with next_run <= now → fires it
    def test_G2_due_schedule_fired(self):
        past = _iso(_utcnow() - timedelta(seconds=5))
        entry = _make_entry(next_run=past)
        self._write_schedules([entry])
        self._fire()
        self.mock_submit.assert_called_once()

    # G3 — future schedule → not fired, returns correct sleep time
    def test_G3_future_schedule_not_fired(self):
        future = _iso(_utcnow() + timedelta(seconds=30))
        entry = _make_entry(next_run=future)
        self._write_schedules([entry])
        result = self._fire()
        self.mock_submit.assert_not_called()
        self.assertLessEqual(result, 30.0)
        self.assertGreater(result, 0.0)

    # G4 — non-active status → not fired
    def test_G4_non_active_not_fired(self):
        for status in ("completed", "running", "error", "limit_reached", "paused"):
            self._write_schedules([_make_entry(status=status, next_run=_iso(_utcnow() - timedelta(seconds=1)))])
            with patch.object(self.mgr._executor, "submit") as mock_sub:
                mock_sub.return_value = MagicMock()
                self.mgr._fire_due()
            mock_sub.assert_not_called(), f"status={status} should not fire"

    # G5 — already in _running → not re-fired
    def test_G5_already_running_not_refired(self):
        past = _iso(_utcnow() - timedelta(seconds=1))
        entry = _make_entry(next_run=past)
        self._write_schedules([entry])
        self.mgr._running[entry["id"]] = MagicMock()
        with patch.object(self.mgr._executor, "submit") as mock_sub:
            self.mgr._fire_due()
        mock_sub.assert_not_called()

    # G6 — multiple schedules due at once → all fired
    def test_G6_multiple_due_all_fired(self):
        past = _iso(_utcnow() - timedelta(seconds=1))
        import uuid as _uuid
        entries = [_make_entry(id=str(_uuid.uuid4()), next_run=past) for _ in range(3)]
        self._write_schedules(entries)
        self._fire()
        self.assertEqual(self.mock_submit.call_count, 3)

    # G7 — sleep time capped at _TICK_CAP even for far-future schedules
    def test_G7_sleep_capped_at_tick_cap(self):
        far_future = _iso(_utcnow() + timedelta(hours=10))
        self._write_schedules([_make_entry(next_run=far_future)])
        result = self._fire()
        self.assertLessEqual(result, sm._TICK_CAP)

    # G8 — slot reserved in _running before lock released (no double-fire)
    def test_G8_slot_reserved_atomically(self):
        past = _iso(_utcnow() - timedelta(seconds=1))
        entry = _make_entry(next_run=past)
        self._write_schedules([entry])
        self._fire()
        # After _fire_due, the id must be in _running
        self.assertIn(entry["id"], self.mgr._running)


# ═══════════════════════════════════════════════════════════════════════════════
# H.  _execute — status transitions (run_loop mocked)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecute(_Base):

    def setUp(self):
        super().setUp()
        self.mgr = self._make_manager()
        self.mock_run_loop = MagicMock(return_value=dict(_GOOD_RESULT))
        self._rl_patch = patch.object(sm, "run_loop", self.mock_run_loop)
        self._rl_patch.start()

    def tearDown(self):
        self._rl_patch.stop()
        super().tearDown()

    def _run_execute(self, entry: dict) -> dict:
        self._write_schedules([entry])
        self.mgr._execute(entry["id"])
        entries = self._read_schedules()
        return entries[0] if entries else {}

    # H1 — sets status="running" in schedules.json before run_loop is called
    def test_H1_sets_running_before_call(self):
        seen_status = []

        def capture_call(*args, **kwargs):
            # Read status at the moment run_loop is called
            entries = self._read_schedules()
            if entries:
                seen_status.append(entries[0]["status"])
            return dict(_GOOD_RESULT)

        self.mock_run_loop.side_effect = capture_call
        entry = _make_entry()
        self._write_schedules([entry])
        self.mgr._execute(entry["id"])
        self.assertIn("running", seen_status)

    # H2 — writes loop_state "running" before run_loop call
    def test_H2_loop_state_running_before_call(self):
        seen = []

        def capture_call(*args, **kwargs):
            if self.loop_state_path.exists():
                seen.append(json.loads(self.loop_state_path.read_text()).get("status"))
            return dict(_GOOD_RESULT)

        self.mock_run_loop.side_effect = capture_call
        entry = _make_entry()
        self._write_schedules([entry])
        self.mgr._execute(entry["id"])
        self.assertIn("running", seen)

    # H3 — run_loop called with correct args from settings
    def test_H3_run_loop_called_with_settings(self):
        entry = _make_entry(session_id="sid-123")
        self._run_execute(entry)
        call_kwargs = self.mock_run_loop.call_args.kwargs
        self.assertEqual(call_kwargs["primary_prompt"], "PRIMARY")
        self.assertEqual(call_kwargs["nextLoop_prompt_static"], "STATIC")
        self.assertEqual(call_kwargs["session_id"], "sid-123")

    # H4 — uses remaining_loop_prompts from entry when set (continuation)
    def test_H4_uses_remaining_prompts_when_set(self):
        entry = _make_entry(remaining_loop_prompts=["leftover-A", "leftover-B"])
        self._run_execute(entry)
        call_kwargs = self.mock_run_loop.call_args.kwargs
        self.assertEqual(call_kwargs["nextLoop_prompt_dynamic"], ["leftover-A", "leftover-B"])

    # H5 — falls back to settings.nextLoop_prompt_dynamic when remaining is empty
    def test_H5_falls_back_to_settings_dynamic(self):
        entry = _make_entry(remaining_loop_prompts=[])
        self._run_execute(entry)
        call_kwargs = self.mock_run_loop.call_args.kwargs
        self.assertEqual(call_kwargs["nextLoop_prompt_dynamic"], ["DYN1"])

    # H6 — success + runs_remaining=0 → completed
    def test_H6_success_runs_remaining_zero_completes(self):
        self.mock_run_loop.return_value = dict(_GOOD_RESULT)
        entry = _make_entry(runs_remaining=0)
        updated = self._run_execute(entry)
        self.assertEqual(updated["status"], "completed")

    # H7 — success + runs_remaining=2 → active, decremented
    def test_H7_success_repeat_active_decremented(self):
        self.mock_run_loop.return_value = dict(_GOOD_RESULT)
        entry = _make_entry(runs_remaining=2, repeat=2)
        updated = self._run_execute(entry)
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["runs_remaining"], 1)
        self.assertIsNotNone(updated["next_run"])

    # H8 — limit=1 + no allow_reschedule → limit_reached, remaining surfaced
    def test_H8_limit_no_allow_limit_reached(self):
        self.mock_run_loop.return_value = {
            "success": 0, "limit_exceeded": 1,
            "remaining_loop_prompts": ["A", "B"],
            "reschedule_time": "2026-05-23T16:00:00+00:00",
        }
        entry = _make_entry(allow_reschedule=False)
        updated = self._run_execute(entry)
        self.assertEqual(updated["status"], "limit_reached")
        self.assertEqual(updated["remaining_loop_prompts"], ["A", "B"])

    # H9 — limit=1 + allow_reschedule + reschedule_time → active with new next_run
    def test_H9_limit_allow_reschedule_auto_reschedules(self):
        rt = "2026-05-23T16:00:00+00:00"
        self.mock_run_loop.return_value = {
            "success": 0, "limit_exceeded": 1,
            "remaining_loop_prompts": ["A"],
            "reschedule_time": rt,
        }
        entry = _make_entry(allow_reschedule=True)
        updated = self._run_execute(entry)
        self.assertEqual(updated["status"], "active")
        self.assertEqual(_parse_iso(updated["next_run"]), _parse_iso(rt))

    # H10 — weekly limit (2) → always limit_reached regardless of allow_reschedule
    def test_H10_weekly_limit_always_limit_reached(self):
        self.mock_run_loop.return_value = {
            "success": 0, "limit_exceeded": 2,
            "remaining_loop_prompts": ["X"],
            "reschedule_time": None,
        }
        entry = _make_entry(allow_reschedule=True)
        updated = self._run_execute(entry)
        self.assertEqual(updated["status"], "limit_reached")

    # H11 — cancellation while running → entry removed, results discarded
    def test_H11_cancellation_discards_results(self):
        def cancel_mid_call(*args, **kwargs):
            self.mgr._cancelled.add("test-sched-id")
            return dict(_GOOD_RESULT)

        self.mock_run_loop.side_effect = cancel_mid_call
        entry = _make_entry()
        self._write_schedules([entry])
        self.mgr._execute(entry["id"])
        # Entry must be removed
        self.assertEqual(self._read_schedules(), [])
        # loop_state must be "idle"
        state = self._read_loop_state()
        self.assertEqual(state.get("status"), "idle")

    # H12 — session_id recovered from runs.json after run_loop
    def test_H12_session_id_recovered_from_runs(self):
        self._write_runs([{"session_id": "recovered-sid"}])
        entry = _make_entry()
        updated = self._run_execute(entry)
        self.assertEqual(updated["session_id"], "recovered-sid")

    # H13 — last_result written to entry
    def test_H13_last_result_written(self):
        self.mock_run_loop.return_value = dict(_GOOD_RESULT)
        updated = self._run_execute(_make_entry())
        self.assertEqual(updated["last_result"]["success"], 1)
        self.assertEqual(updated["last_result"]["limit_exceeded"], 0)

    # H14 — continuing_from_limit_reached set on entry when limit hit with remaining
    def test_H14_continuing_flag_set_after_limit(self):
        self.mock_run_loop.return_value = {
            "success": 0, "limit_exceeded": 1,
            "remaining_loop_prompts": ["A"],
            "reschedule_time": None,
        }
        entry = _make_entry(allow_reschedule=False)
        updated = self._run_execute(entry)
        self.assertTrue(updated["continuing_from_limit_reached"])

    # H15 — run_loop exception → _run_schedule still cleans up _running
    def test_H15_run_loop_exception_cleanup(self):
        self.mock_run_loop.side_effect = RuntimeError("unexpected crash")
        entry = _make_entry()
        self._write_schedules([entry])
        self.mgr._running[entry["id"]] = MagicMock()  # pre-register
        # _run_schedule uses try/finally so exception propagates; cleanup still runs
        with self.assertRaises(RuntimeError):
            self.mgr._run_schedule(entry["id"])
        self.assertNotIn(entry["id"], self.mgr._running)

    # H16 — entry not found in schedules.json → _execute returns without error
    def test_H16_missing_entry_no_crash(self):
        # schedules.json is empty
        self.mgr._execute("nonexistent-id")
        self.mock_run_loop.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# I.  Singleton — get_manager thread-safety
# ═══════════════════════════════════════════════════════════════════════════════

class TestSingleton(_Base):

    def setUp(self):
        super().setUp()
        # Reset module-level singleton before each test
        with patch.object(sm, "_manager_lock", threading.Lock()):
            sm._manager = None

    def tearDown(self):
        if sm._manager is not None:
            sm._manager.shutdown()
            sm._manager = None
        super().tearDown()

    # I1 — get_manager returns same instance every time
    def test_I1_same_instance(self):
        m1 = get_manager()
        m2 = get_manager()
        self.assertIs(m1, m2)

    # I2 — get_manager from multiple threads returns same singleton
    def test_I2_thread_safe_singleton(self):
        results = []

        def fetch():
            results.append(get_manager())

        threads = [threading.Thread(target=fetch) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(len(results), 10)
        self.assertEqual(len(set(id(r) for r in results)), 1)

    # I3 — tick thread is running after get_manager
    def test_I3_tick_thread_started(self):
        mgr = get_manager()
        self.assertTrue(mgr._tick_thread.is_alive())

    # I4 — shutdown stops tick thread
    def test_I4_shutdown_stops_tick_thread(self):
        mgr = get_manager()
        sm._manager = None  # allow re-creation next time
        mgr.shutdown()
        # Give the thread a moment
        mgr._tick_thread.join(timeout=2)
        self.assertFalse(mgr._tick_thread.is_alive())


# ═══════════════════════════════════════════════════════════════════════════════
# J.  End-to-end with real ScheduleManager tick thread (no Claude)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEndToEnd(_Base):
    """
    Starts the tick thread for real but mocks run_loop.
    Verifies the scheduler fires a 'now' schedule and updates status.
    """

    def setUp(self):
        super().setUp()
        self.mock_run_loop_fn = MagicMock(return_value=dict(_GOOD_RESULT))
        self._rl_patch = patch.object(sm, "run_loop", self.mock_run_loop_fn)
        self._rl_patch.start()
        self.mgr = self._make_manager(start=True)

    def tearDown(self):
        self.mgr.shutdown()
        self._rl_patch.stop()
        super().tearDown()

    # J1 — schedule now=True fires and completes within 5 seconds
    def test_J1_immediate_schedule_fires(self):
        self.mgr.schedule_loop({
            "primary_prompt": "P",
            "nextLoop_prompt_static": "S",
            "nextLoop_prompt_dynamic": ["D"],
            "now": True,
        })
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            entries = self._read_schedules()
            if entries and entries[0]["status"] in ("completed", "error", "limit_reached"):
                break
            time.sleep(0.1)
        self.assertTrue(bool(entries), "schedule entry must exist")
        self.assertEqual(entries[0]["status"], "completed")
        self.mock_run_loop_fn.assert_called_once()

    # J2 — two concurrent now=True schedules both complete
    def test_J2_two_concurrent_schedules(self):
        import uuid as _uuid
        slow_result = dict(_GOOD_RESULT)
        call_count = [0]

        def slow_call(**kwargs):
            call_count[0] += 1
            time.sleep(0.05)
            return slow_result

        self.mock_run_loop_fn.side_effect = slow_call

        config = {
            "primary_prompt": "P",
            "nextLoop_prompt_static": "S",
            "nextLoop_prompt_dynamic": ["D"],
            "now": True,
        }
        self.mgr.schedule_loop(dict(config))
        self.mgr.schedule_loop(dict(config))

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            entries = self._read_schedules()
            done = [e for e in entries if e["status"] in ("completed", "error")]
            if len(done) == 2:
                break
            time.sleep(0.1)

        self.assertEqual(len(done), 2)
        self.assertEqual(call_count[0], 2)

    # J3 — cancel while idle prevents execution
    def test_J3_cancel_idle_prevents_execution(self):
        # Schedule far in future so tick thread won't fire it
        future = _iso(_utcnow() + timedelta(hours=10))
        config = {
            "primary_prompt": "P",
            "nextLoop_prompt_static": "S",
            "nextLoop_prompt_dynamic": ["D"],
            "hour_utc": (_utcnow() + timedelta(hours=10)).hour,
        }
        sched_id = self.mgr.schedule_loop(config)
        time.sleep(0.05)  # let tick thread process
        self.mgr.cancel_schedule(sched_id)
        time.sleep(0.1)
        self.assertEqual(self._read_schedules(), [])
        self.mock_run_loop_fn.assert_not_called()


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
