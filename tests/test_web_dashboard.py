#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    tests/test_web_dashboard.py
# Description: Unit tests for web_dashboard — NaN/Infinity scrubbing and the live
#              server, driven on an ephemeral loopback port.
# Author:      CliveS & Claude Opus 5
# Date:        20-09-2026
# Version:     1.0

import json
import logging
import socket
import unittest
import urllib.error
import urllib.request

from web_dashboard import WebDashboard, _json_safe


class TestJsonSafe(unittest.TestCase):
    """json.dumps happily writes NaN and Infinity; the browser's JSON.parse then
    throws and the whole dashboard goes blank. A Modbus register that reads back
    unscaled is exactly how a NaN gets in."""

    def test_nan_and_infinities_become_none(self):
        for v in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(v=v):
                self.assertIsNone(_json_safe(v))

    def test_ordinary_values_pass_through(self):
        for v in (0.0, -1.5, 42, "text", True, None):
            with self.subTest(v=v):
                self.assertEqual(_json_safe(v), v)

    def test_it_reaches_inside_nested_structures(self):
        out = _json_safe({"inv": {"soc": float("nan"), "pv": 1200},
                          "hist": [1.0, float("inf"), 3.0]})
        self.assertEqual(out, {"inv": {"soc": None, "pv": 1200},
                               "hist": [1.0, None, 3.0]})

    def test_the_result_is_serialisable_without_the_non_standard_tokens(self):
        # THE point: json.dumps would otherwise emit bare NaN, which is not JSON.
        body = json.dumps(_json_safe({"a": float("nan"), "b": [float("inf")]}))
        self.assertNotIn("NaN", body)
        self.assertNotIn("Infinity", body)
        self.assertEqual(json.loads(body), {"a": None, "b": [None]})


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestServer(unittest.TestCase):
    def setUp(self):
        self.status = {"ok": True, "phase": "idle", "inverter": {"soc_pct": 88.5}}
        log = logging.getLogger("test.web")
        log.addHandler(logging.NullHandler())
        self.port = _free_port()
        self.web = WebDashboard(lambda: self.status, host="127.0.0.1",
                                port=self.port, logger=log)
        self.web.start()
        self.addCleanup(self.web.stop)

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.read(), r.headers.get("Content-Type")

    def test_status_returns_the_live_dict(self):
        code, body, ctype = self._get("/api/status")
        self.assertEqual(code, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(json.loads(body), self.status)

    def test_status_is_read_fresh_on_every_request(self):
        # The provider is a callable, not a snapshot taken at start(). If this ever
        # regressed the dashboard would show the state at daemon startup for ever.
        self._get("/api/status")
        self.status["phase"] = "active"
        self.assertEqual(json.loads(self._get("/api/status")[1])["phase"], "active")

    def test_a_nan_from_the_provider_does_not_reach_the_browser(self):
        self.status["inverter"]["soc_pct"] = float("nan")
        body = self._get("/api/status")[1].decode()
        self.assertNotIn("NaN", body)
        self.assertIsNone(json.loads(body)["inverter"]["soc_pct"])

    def test_a_throwing_provider_answers_an_error_rather_than_hanging(self):
        # The daemon's own state lock could raise, or the dict could hold something
        # unserialisable. A 200 with ok:false keeps the page alive and says why.
        def boom():
            raise RuntimeError("state unavailable")
        port = _free_port()
        log = logging.getLogger("test.web")
        web = WebDashboard(boom, host="127.0.0.1", port=port, logger=log)
        web.start()
        self.addCleanup(web.stop)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status",
                                    timeout=5) as r:
            payload = json.loads(r.read())
        self.assertFalse(payload["ok"])
        self.assertIn("state unavailable", payload["error"])

    def test_the_index_page_is_served(self):
        code, body, ctype = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"SigenVPP", body)

    def test_an_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/../config.json")
        self.assertEqual(cm.exception.code, 404)

    def test_it_binds_loopback_only(self):
        # config.json can widen this deliberately; the DEFAULT must never publish
        # SOC, grid flow and the VPP schedule to the LAN with no authentication.
        self.assertEqual(self.web.host, "127.0.0.1")

    def test_stop_is_safe_before_start_and_twice(self):
        w = WebDashboard(lambda: {}, port=_free_port())
        w.stop()
        w.start()
        w.stop()
        w.stop()


if __name__ == "__main__":
    unittest.main()
