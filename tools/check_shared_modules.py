#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    check_shared_modules.py
# Description: Detect (and optionally repair) drift between SigenVPP's copies of
#              the shared Sigenergy modules and the SigenEnergyManager master
# Author:      CliveS & Claude Opus 5
# Date:        24-08-2026
# Version:     1.0

"""Guard against silent drift in the modules SigenVPP shares with the Indigo plugin.

SigenVPP carries byte-identical copies of a few modules whose master lives in the
SigenEnergyManager plugin. Copies drift. They have drifted twice already — once
caught by hand in July 2026, and again by August, when this repo's Modbus client
had fallen five versions behind the master and its Axle client two.

Keeping the copies byte-identical is what makes this check a one-line checksum
rather than a diff nobody reads. Any change that only one side needs belongs in
the master too, or the whole scheme collapses back into hand-syncing.

This is a stopgap. The real fix is a single shared core package that both the
plugin and this daemon import. Until that exists, run this before every release.

Exit codes:
    0  every shared module matches the master
    1  drift found (or repaired, with --sync)
    2  the master could not be read, so nothing was checked

Note the third code. A check that cannot run must not report success.

Usage:
    python3 tools/check_shared_modules.py           # report only
    python3 tools/check_shared_modules.py --sync    # copy master over the copies

The master location defaults to the standard clone path and can be overridden:
    SIGEN_PLUGIN_SRC=/path/to/Server\\ Plugin python3 tools/check_shared_modules.py
"""

import hashlib
import os
import shutil
import sys

# Modules whose master is the SigenEnergyManager plugin. Add to this list when a
# further module is shared — one line here is the whole registration.
SHARED_MODULES = [
    "sigenergy_modbus.py",
    "axle_api.py",
]

DEFAULT_MASTER = os.path.expanduser(
    "~/GitHub/SigenEnergyManager/SigenEnergyManager.indigoPlugin/Contents/Server Plugin"
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def digest(path):
    """Return the SHA-256 of a file, or None if it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def main(argv):
    sync = "--sync" in argv
    master_dir = os.environ.get("SIGEN_PLUGIN_SRC", DEFAULT_MASTER)

    if not os.path.isdir(master_dir):
        print(f"NOT CHECKED — no master at {master_dir}")
        print("Set SIGEN_PLUGIN_SRC to the plugin's 'Server Plugin' folder.")
        return 2

    drifted = []
    unreadable = []

    for name in SHARED_MODULES:
        master_path = os.path.join(master_dir, name)
        local_path = os.path.join(REPO_ROOT, name)

        master_sum = digest(master_path)
        local_sum = digest(local_path)

        if master_sum is None:
            unreadable.append((name, master_path))
            continue

        if master_sum == local_sum:
            print(f"  match   {name}")
            continue

        drifted.append(name)
        state = "MISSING" if local_sum is None else "DIFFERS"
        print(f"  {state} {name}")

        if sync:
            shutil.copy2(master_path, local_path)
            print(f"          copied master -> {local_path}")

    if unreadable:
        for name, path in unreadable:
            print(f"NOT CHECKED — cannot read master {path}")
        return 2

    if not drifted:
        print(f"\nAll {len(SHARED_MODULES)} shared modules match the master.")
        return 0

    if sync:
        print(f"\nRepaired {len(drifted)} module(s). Re-run the tests, then commit.")
    else:
        print(f"\n{len(drifted)} module(s) have drifted from the master.")
        print("Run with --sync to copy the master over them, then re-run the tests.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
