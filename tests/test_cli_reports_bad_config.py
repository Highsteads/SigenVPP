#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    tests/test_cli_reports_bad_config.py
# Description: main() must turn a ConfigError into one readable line and exit 1,
#              for every command. The exception being raised is only half the fix;
#              this is the half the user actually sees.
# Author:      CliveS & Claude Opus 5
# Date:        20-09-2026
# Version:     1.0

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import config as cfgmod
import sigen_vpp


class TestCliReportsBadConfig(unittest.TestCase):
    def _run(self, argv):
        out = io.StringIO()
        with patch.object(sigen_vpp.sys, "argv", ["sigen_vpp.py"] + argv), \
             redirect_stdout(out):
            rc = sigen_vpp.main()
        return rc, out.getvalue()

    def test_every_command_reports_it_rather_than_raising(self):
        # One try/except in main() covers all four; this is what proves it, and
        # what would fail if a fifth command were added outside the block.
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")

        with patch.object(cfgmod, "config_path", return_value=path):
            for cmd in ("--status", "--run", "--test-export", "--setup"):
                with self.subTest(cmd):
                    rc, out = self._run([cmd])
                    self.assertEqual(rc, 1)
                    self.assertIn("Configuration problem", out)
                    self.assertIn(path, out)
                    self.assertNotIn("Traceback", out)

    def test_the_message_tells_the_user_what_to_do(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2]")
        with patch.object(cfgmod, "config_path", return_value=path):
            rc, out = self._run(["--status"])
        self.assertEqual(rc, 1)
        self.assertIn("--setup", out)

    def test_a_missing_config_still_gets_its_own_friendlier_message(self):
        # Absent is not corrupt — it must keep saying "run --setup", not
        # "Configuration problem".
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        with patch.object(cfgmod, "config_path", return_value=path):
            rc, out = self._run(["--status"])
        self.assertEqual(rc, 1)
        self.assertNotIn("Configuration problem", out)
        self.assertIn("--setup", out)


if __name__ == "__main__":
    unittest.main()
