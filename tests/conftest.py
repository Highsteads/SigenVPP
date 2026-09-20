#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    tests/conftest.py
# Description: Shared test setup for SigenVPP — puts the repo root on sys.path.
# Author:      CliveS & Claude Opus 5
# Date:        20-09-2026
# Version:     1.0
#
# WHAT THIS SUITE DELIBERATELY DOES NOT COVER, AND WHY
#
# axle_api.py and sigenergy_modbus.py are SHARED modules. Their master lives in
# SigenEnergyManager and is held byte-identical here by tools/check_shared_modules.py,
# which CI runs against a checkout of that repo on every push. They already have
# 31 and several hundred tests THERE, against the master.
#
# Copying those tests here would create a second copy to drift, and the half that
# drifts is always the one nobody watches. So this suite covers only what is unique
# to the daemon: config handling, the export controller, the window arithmetic and
# the dashboard's JSON safety.
#
# NOTHING HERE TOUCHES HARDWARE. The controller is driven against a recording fake
# in place of the Modbus client, so a test can assert on what WOULD be written to
# the inverter without a Sigenergy being present.

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
