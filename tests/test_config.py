#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    tests/test_config.py
# Description: Unit tests for config.py — defaults, deep merge, load/save round trip
#              the 0600 permissions that protect the Axle token, and the two
#              v0.2 fixes: whitespace trimming and a named ConfigError.
# Author:      CliveS & Claude Opus 5
# Date:        20-09-2026
# Version:     1.1

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


class TestTrimsPastedFields(unittest.TestCase):
    """A token copied out of a browser routinely carries a trailing newline.
    Trimming happens on LOAD, not at the check — trimming only at the check would
    let setup report ready while the daemon still sent the untrimmed value."""

    def _loaded(self, user):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(user, fh)
            return cfgmod.load_config(p)[0]

    def test_the_token_and_ip_are_trimmed(self):
        cfg = self._loaded({"inverter": {"ip": "  192.168.1.50\n"},
                            "vpp": {"axle_token": "\ttok  "}})
        self.assertEqual(cfg["inverter"]["ip"], "192.168.1.50")
        self.assertEqual(cfg["vpp"]["axle_token"], "tok")

    def test_pushover_keys_are_trimmed_too(self):
        cfg = self._loaded({"notify": {"pushover_token": " a\n", "pushover_user": "b "}})
        self.assertEqual(cfg["notify"]["pushover_token"], "a")
        self.assertEqual(cfg["notify"]["pushover_user"], "b")

    def test_a_whitespace_only_token_becomes_empty_so_the_check_fails(self):
        cfg = self._loaded({"inverter": {"ip": "192.168.1.50"},
                            "vpp": {"axle_token": "   \n"}})
        self.assertEqual(cfg["vpp"]["axle_token"], "")
        self.assertFalse(cfgmod.is_configured(cfg))

    def test_non_string_values_are_left_alone(self):
        cfg = self._loaded({"inverter": {"ip": 123}})
        self.assertEqual(cfg["inverter"]["ip"], 123)

    def test_other_fields_are_not_trimmed(self):
        # Only the hand-pasted ones. A deliberate trailing space elsewhere is the
        # user's business.
        cfg = self._loaded({"web": {"bind": " 0.0.0.0 "}})
        self.assertEqual(cfg["web"]["bind"], " 0.0.0.0 ")


class TestConfigError(unittest.TestCase):
    """A config that exists but cannot be used must name itself and refuse to
    start. It must NEVER fall back to DEFAULTS — that would silently drive the
    inverter to a 4 kW target and a 10% reserve nobody chose."""

    def _load(self, text, mode="w"):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "config.json")
        with open(p, mode) as fh:
            fh.write(text)
        return p

    def test_malformed_json_names_the_file_and_the_place(self):
        p = self._load('{"vpp": {"axle_token": "abc",}')
        with self.assertRaises(cfgmod.ConfigError) as cm:
            cfgmod.load_config(p)
        msg = str(cm.exception)
        self.assertIn(p, msg)
        self.assertIn("line 1", msg)
        self.assertIn("--setup", msg)

    def test_the_original_error_is_kept_as_the_cause(self):
        p = self._load("{not json")
        with self.assertRaises(cfgmod.ConfigError) as cm:
            cfgmod.load_config(p)
        self.assertIsInstance(cm.exception.__cause__, json.JSONDecodeError)

    def test_valid_json_that_is_not_an_object_is_refused(self):
        for label, text in (("a list", "[1, 2]"), ("a string", '"hello"'),
                            ("null", "null"), ("a number", "42")):
            with self.subTest(label):
                p = self._load(text)
                with self.assertRaises(cfgmod.ConfigError) as cm:
                    cfgmod.load_config(p)
                self.assertIn("JSON object", str(cm.exception))

    def test_a_file_that_is_not_utf8_is_refused(self):
        p = self._load(b"\xff\xfe{}", mode="wb")
        with self.assertRaises(cfgmod.ConfigError) as cm:
            cfgmod.load_config(p)
        self.assertIn("UTF-8", str(cm.exception))

    def test_an_unreadable_file_is_refused_with_its_reason(self):
        p = self._load("{}")
        os.chmod(p, 0o000)
        self.addCleanup(os.chmod, p, 0o600)
        if os.geteuid() == 0:
            self.skipTest("running as root — permissions are not enforced")
        with self.assertRaises(cfgmod.ConfigError) as cm:
            cfgmod.load_config(p)
        self.assertIn(p, str(cm.exception))

    def test_it_never_silently_falls_back_to_defaults(self):
        # THE point. A corrupt file returning (DEFAULTS, True) would start the
        # daemon on an export target and reserve floor the user never chose.
        p = self._load("{ broken")
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config(p)

    def test_a_missing_file_is_still_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, existed = cfgmod.load_config(os.path.join(d, "absent.json"))
        self.assertFalse(existed)
        self.assertEqual(cfg["vpp"]["export_target_kw"], 4.0)


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
        # Was a real flaw until v0.2: a token of stray whitespace passed the check
        # and then got a 401 from Axle, so setup said ready and nothing worked.
        for label, ip, token in (
            ("space token",   "192.168.1.50", " "),
            ("newline token", "192.168.1.50", "\n"),
            ("tab ip",        "\t",           "tok"),
        ):
            with self.subTest(label):
                self.assertFalse(cfgmod.is_configured(self._cfg(ip, token)))

    def test_a_non_dict_section_is_not_configured(self):
        # A hand-edited file can replace a whole section with a scalar. Indexing
        # it would raise AttributeError out of a boolean check.
        self.assertFalse(cfgmod.is_configured({"inverter": "", "vpp": {"axle_token": "t"}}))
        self.assertFalse(cfgmod.is_configured({}))

    def test_a_non_string_value_does_not_explode(self):
        self.assertTrue(cfgmod.is_configured(
            {"inverter": {"ip": 19216815}, "vpp": {"axle_token": "tok"}}))


if __name__ == "__main__":
    unittest.main()
