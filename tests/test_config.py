#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    tests/test_config.py
# Description: Unit tests for config.py — defaults, deep merge, load/save round trip
#              and the 0600 permissions that protect the Axle token.
# Author:      CliveS & Claude Opus 5
# Date:        20-09-2026
# Version:     1.0

import json
import os
import stat
import tempfile
import unittest

import config as cfgmod


class TestDeepMerge(unittest.TestCase):
    def test_override_wins_leaf_by_leaf(self):
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        out = cfgmod._deep_merge(base, {"a": {"y": 99}})
        self.assertEqual(out, {"a": {"x": 1, "y": 99}, "b": 3})

    def test_the_base_is_not_mutated(self):
        # load_config merges over the module-level DEFAULTS dict on every call. If
        # the merge mutated it, the SECOND load would inherit the first user's
        # settings — and in a daemon that reloads config, that is a config that
        # silently never resets.
        base = {"a": {"x": 1}}
        cfgmod._deep_merge(base, {"a": {"x": 2}})
        self.assertEqual(base, {"a": {"x": 1}})

    def test_defaults_are_not_mutated_by_a_load(self):
        before = json.dumps(cfgmod.DEFAULTS, sort_keys=True)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"vpp": {"export_target_kw": 7.5}}, fh)
            cfgmod.load_config(p)
            cfgmod.load_config(p)
        self.assertEqual(before, json.dumps(cfgmod.DEFAULTS, sort_keys=True))

    def test_none_override_is_tolerated(self):
        self.assertEqual(cfgmod._deep_merge({"a": 1}, None), {"a": 1})

    def test_a_scalar_replaces_a_dict(self):
        # Not a nicety: a hand-edited config.json that writes "vpp": "" must not
        # merge into the defaults dict and pretend to be configured.
        out = cfgmod._deep_merge({"a": {"x": 1}}, {"a": ""})
        self.assertEqual(out, {"a": ""})


class TestLoadConfig(unittest.TestCase):
    def test_missing_file_gives_defaults_and_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, existed = cfgmod.load_config(os.path.join(d, "nope.json"))
        self.assertFalse(existed)
        self.assertEqual(cfg["inverter"]["port"], 502)
        self.assertEqual(cfg["vpp"]["export_target_kw"], 4.0)

    def test_user_values_win_and_the_rest_survive(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"vpp": {"export_target_kw": 3.68}}, fh)
            cfg, existed = cfgmod.load_config(p)
        self.assertTrue(existed)
        self.assertEqual(cfg["vpp"]["export_target_kw"], 3.68)
        # an untouched sibling key is still there — the merge is not a replace
        self.assertEqual(cfg["vpp"]["reserve_floor_pct"], 10.0)
        self.assertEqual(cfg["web"]["bind"], "127.0.0.1")

    def test_a_partial_file_never_loses_a_required_key(self):
        # is_configured() and cmd_run() index these directly, so a missing key is
        # a KeyError in the daemon's startup path rather than a clear message.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"web": {"port": 9999}}, fh)
            cfg, _ = cfgmod.load_config(p)
        for section, key in (("inverter", "ip"), ("vpp", "axle_token"),
                             ("vpp", "poll_interval_min"), ("web", "bind")):
            self.assertIn(key, cfg[section], f"{section}.{key} lost by the merge")


class TestSaveConfig(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            cfg, _ = cfgmod.load_config(p)
            cfg["inverter"]["ip"] = "192.168.1.50"
            cfgmod.save_config(cfg, p)
            back, existed = cfgmod.load_config(p)
        self.assertTrue(existed)
        self.assertEqual(back["inverter"]["ip"], "192.168.1.50")

    def test_written_file_is_owner_only(self):
        # THE point of this test: config.json holds the Axle bearer token and any
        # Pushover keys. World-readable would publish them to every account on the
        # machine, and nothing else in the daemon checks.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            cfg, _ = cfgmod.load_config(p)
            cfgmod.save_config(cfg, p)
            mode = stat.S_IMODE(os.stat(p).st_mode)
        self.assertEqual(mode, 0o600, f"config.json is {oct(mode)}, not 0600")

    def test_file_ends_with_a_newline(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            cfg, _ = cfgmod.load_config(p)
            cfgmod.save_config(cfg, p)
            self.assertTrue(open(p, encoding="utf-8").read().endswith("\n"))


class TestIsConfigured(unittest.TestCase):
    def _cfg(self, ip, token):
        cfg, _ = cfgmod.load_config(os.path.join(tempfile.gettempdir(), "nope.json"))
        cfg["inverter"]["ip"] = ip
        cfg["vpp"]["axle_token"] = token
        return cfg

    def test_needs_both(self):
        for label, ip, token, want in (
            ("neither",    "",             "",      False),
            ("ip only",    "192.168.1.50", "",      False),
            ("token only", "",             "tok",   False),
            ("both",       "192.168.1.50", "tok",   True),
        ):
            with self.subTest(label):
                self.assertEqual(cfgmod.is_configured(self._cfg(ip, token)), want)

    def test_whitespace_is_not_a_token(self):
        # Documents CURRENT behaviour: a config whose token is a stray space passes
        # is_configured() and then fails at Axle with a 401. Flagged, not silently
        # "fixed" here — changing it is a behaviour change, not a test.
        self.assertTrue(cfgmod.is_configured(self._cfg("192.168.1.50", " ")))


if __name__ == "__main__":
    unittest.main()
