#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    tests/test_window_phase.py
# Description: Unit tests for sigen_vpp.window_phase — when the daemon starts and
#              stops forcing the inverter. Pure arithmetic, no clock, no hardware.
# Author:      CliveS & Claude Opus 5
# Date:        20-09-2026
# Version:     1.0

import unittest
from datetime import datetime, timedelta, timezone

from sigen_vpp import window_phase

LEAD, TRAIL, PRE = 2, 2, 30     # the shipped defaults


def _event(start, hours=1.0):
    end = start + timedelta(hours=hours)
    return {"start_time": start, "end_time": end, "duration_hrs": hours,
            "import_export": "export"}


def _utc(y, m, d, hh, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


class TestNoEvent(unittest.TestCase):
    def test_none_is_quiet(self):
        self.assertEqual(window_phase(None, _utc(2026, 6, 15, 12), LEAD, TRAIL, PRE),
                         (False, False, None))


class TestBoundaries(unittest.TestCase):
    """The edges are where a control loop goes wrong, so every one is pinned."""

    def setUp(self):
        self.start = _utc(2026, 6, 15, 18)
        self.ev = _event(self.start)              # 18:00-19:00 UTC

    def _at(self, when):
        return window_phase(self.ev, when, LEAD, TRAIL, PRE)[:2]

    def test_long_before_is_neither(self):
        self.assertEqual(self._at(self.start - timedelta(hours=3)), (False, False))

    def test_precharge_opens_exactly_on_its_boundary(self):
        self.assertEqual(self._at(self.start - timedelta(minutes=PRE)), (False, True))

    def test_one_second_before_precharge_is_neither(self):
        self.assertEqual(
            self._at(self.start - timedelta(minutes=PRE, seconds=1)), (False, False))

    def test_driving_opens_exactly_at_lead(self):
        self.assertEqual(self._at(self.start - timedelta(minutes=LEAD)), (True, False))

    def test_the_last_second_of_precharge_is_still_precharge(self):
        self.assertEqual(
            self._at(self.start - timedelta(minutes=LEAD, seconds=1)), (False, True))

    def test_mid_event_is_driving(self):
        self.assertEqual(self._at(self.start + timedelta(minutes=30)), (True, False))

    def test_the_last_second_of_the_trail_still_drives(self):
        end = self.ev["end_time"]
        self.assertEqual(self._at(end + timedelta(minutes=TRAIL, seconds=-1)),
                         (True, False))

    def test_driving_releases_exactly_at_end_plus_trail(self):
        # Half-open on purpose: the tick that REACHES this boundary is already out,
        # so the window cannot be held open by a tick landing exactly on it.
        end = self.ev["end_time"]
        self.assertEqual(self._at(end + timedelta(minutes=TRAIL)), (False, False))

    def test_the_two_phases_are_mutually_exclusive(self):
        t = self.start - timedelta(minutes=PRE)
        while t < self.ev["end_time"] + timedelta(minutes=TRAIL + 5):
            w, p, _ = window_phase(self.ev, t, LEAD, TRAIL, PRE)
            self.assertFalse(w and p, f"both phases true at {t}")
            t += timedelta(seconds=30)


class TestBothSeasons(unittest.TestCase):
    """Two seasons, not one — the standing rule for anything carrying a timezone.
    The arithmetic is all UTC, so a BST event must behave exactly like a GMT one;
    only the human string differs."""

    def _probe(self, start):
        ev = _event(start)
        before = window_phase(ev, start - timedelta(minutes=LEAD, seconds=1),
                              LEAD, TRAIL, PRE)
        at = window_phase(ev, start - timedelta(minutes=LEAD), LEAD, TRAIL, PRE)
        return before[:2], at[:2]

    def test_bst_event(self):
        before, at = self._probe(_utc(2026, 6, 15, 17))    # 18:00 BST
        self.assertEqual((before, at), (((False, True)), ((True, False))))

    def test_gmt_event(self):
        before, at = self._probe(_utc(2026, 1, 15, 18))    # 18:00 GMT
        self.assertEqual((before, at), (((False, True)), ((True, False))))

    def test_an_event_spanning_the_spring_forward(self):
        # 2026-03-29 01:00 UTC, the hour the UK clocks jump to BST. The window is
        # an hour long in UTC whatever the local clock says it is.
        ev = _event(_utc(2026, 3, 29, 0, 30))
        self.assertEqual(window_phase(ev, _utc(2026, 3, 29, 1), LEAD, TRAIL, PRE)[0],
                         True)
        self.assertEqual(window_phase(ev, _utc(2026, 3, 29, 1, 35), LEAD, TRAIL, PRE)[0],
                         False)

    def test_the_local_string_reflects_the_season(self):
        _, _, summer = window_phase(_event(_utc(2026, 6, 15, 17)),
                                    _utc(2026, 6, 15, 12), LEAD, TRAIL, PRE)
        _, _, winter = window_phase(_event(_utc(2026, 1, 15, 18)),
                                    _utc(2026, 1, 15, 12), LEAD, TRAIL, PRE)
        self.assertIn("18:00", summer["local"])    # 17:00 UTC shown as 18:00 BST
        self.assertIn("18:00", winter["local"])    # 18:00 UTC shown as 18:00 GMT


class TestEventState(unittest.TestCase):
    def setUp(self):
        self.start = _utc(2026, 6, 15, 18)
        self.ev = _event(self.start, hours=1.5)

    def test_countdown_reaches_zero_and_never_goes_negative(self):
        _, _, s = window_phase(self.ev, self.start - timedelta(minutes=LEAD + 10),
                               LEAD, TRAIL, PRE)
        self.assertEqual(s["starts_in_s"], 600)
        _, _, s = window_phase(self.ev, self.start + timedelta(hours=1),
                               LEAD, TRAIL, PRE)
        self.assertEqual(s["starts_in_s"], 0)

    def test_active_flag_matches_the_returned_phase(self):
        for when in (self.start - timedelta(hours=2), self.start,
                     self.start + timedelta(hours=9)):
            with self.subTest(when=when):
                w, _, s = window_phase(self.ev, when, LEAD, TRAIL, PRE)
                self.assertEqual(s["active"], w)

    def test_timestamps_are_iso_and_duration_is_rounded(self):
        _, _, s = window_phase(self.ev, self.start, LEAD, TRAIL, PRE)
        self.assertEqual(s["start"], self.start.isoformat())
        self.assertEqual(s["duration_hrs"], 1.5)


class TestConfigurableLeads(unittest.TestCase):
    def test_zero_lead_starts_exactly_on_time(self):
        start = _utc(2026, 6, 15, 18)
        ev = _event(start)
        self.assertEqual(window_phase(ev, start, 0, 0, PRE)[0], True)
        self.assertEqual(
            window_phase(ev, start - timedelta(seconds=1), 0, 0, PRE)[0], False)

    def test_a_precharge_shorter_than_the_lead_leaves_no_precharge_phase(self):
        # Documents the consequence rather than asserting it is wanted: with
        # precharge_lead_min <= start_lead_min the precharge window is empty, so
        # the reserve floor is never raised before the event.
        start = _utc(2026, 6, 15, 18)
        ev = _event(start)
        t = start - timedelta(minutes=10)
        while t <= start:
            self.assertFalse(window_phase(ev, t, 5, TRAIL, 5)[1],
                             f"unexpected precharge at {t}")
            t += timedelta(seconds=15)


if __name__ == "__main__":
    unittest.main()
