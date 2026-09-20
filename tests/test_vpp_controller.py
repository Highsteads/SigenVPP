#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    tests/test_vpp_controller.py
# Description: Unit tests for vpp_controller.VppController — the code that actually
#              forces the inverter during an Axle window. Driven against a recording
#              fake in place of the Modbus client; no hardware, no network.
# Author:      CliveS & Claude Opus 5
# Date:        20-09-2026
# Version:     1.0

import logging
import unittest

import config as cfgmod
from vpp_controller import BANK_CAP_DEADBAND, DAYTIME_PV_W, HYST_W, VppController


class FakeModbus:
    """Records every write the controller makes, in order. A test asserts on this
    list rather than on a log line, so a change of wording cannot break it and a
    change of BEHAVIOUR cannot slip past it."""

    def __init__(self):
        self.calls = []

    def set_self_consumption(self):
        self.calls.append(("self_consumption",))

    def set_charge_limit(self, watts, quiet=False):
        self.calls.append(("charge_limit", watts))

    def set_discharge_cutoff(self, pct):
        self.calls.append(("discharge_cutoff", pct))

    def daytime_export(self, watts):
        self.calls.append(("daytime_export", watts))

    def night_export(self, watts):
        self.calls.append(("night_export", watts))

    def names(self):
        return [c[0] for c in self.calls]


def _cfg(**vpp):
    cfg = cfgmod._deep_merge(cfgmod.DEFAULTS, {})
    cfg["vpp"].update(vpp)
    return cfg


def _snap(pv=0, home=0, grid=0, battery=0, soc=50.0):
    return {"pvPowerWatts": pv, "homePowerWatts": home, "gridPowerWatts": grid,
            "batteryPowerWatts": battery, "batterySoc": soc}


def _ctrl(mb=None, **vpp):
    log = logging.getLogger("test")
    log.addHandler(logging.NullHandler())
    return VppController(mb or FakeModbus(), _cfg(**vpp), log)


class TestConfigDerivedWatts(unittest.TestCase):
    def test_kw_to_w(self):
        c = _ctrl(export_target_kw=3.68, inverter_max_kw=10.0)
        self.assertEqual(c.target_w, 3680)
        self.assertEqual(c.inv_max_w, 10000)

    def test_a_string_from_hand_edited_json_still_works(self):
        c = _ctrl(export_target_kw="4.0", inverter_max_kw="10")
        self.assertEqual(c.target_w, 4000)
        self.assertEqual(c.inv_max_w, 10000)


class TestSubModeChoice(unittest.TestCase):
    """The hysteresis table. One-sided on purpose: a drop below target ALWAYS falls
    back to discharge so the full export is guaranteed, while entering bank needs
    headroom so a PV spike cannot flap the inverter."""

    def _sub(self, surplus, prev=None, target=4000):
        c = _ctrl(export_target_kw=target / 1000.0)
        c._submode = prev
        # home 0 so surplus == pv; pv also decides day/night, which this test ignores
        return c.drive(_snap(pv=surplus, home=0))["submode"]

    def test_below_target_is_always_discharge(self):
        for prev in (None, "bank", "discharge"):
            with self.subTest(prev=prev):
                self.assertEqual(self._sub(3999, prev), "discharge")

    def test_well_above_target_is_always_bank(self):
        for prev in (None, "bank", "discharge"):
            with self.subTest(prev=prev):
                self.assertEqual(self._sub(4000 + HYST_W, prev), "bank")

    def test_the_hold_band_keeps_the_previous_mode(self):
        # [target, target+HYST) — this is the band that stops the flapping.
        mid = 4000 + HYST_W // 2
        self.assertEqual(self._sub(mid, "bank"), "bank")
        self.assertEqual(self._sub(mid, "discharge"), "discharge")

    def test_the_hold_band_with_no_history_falls_to_discharge(self):
        # Guaranteeing the export beats banking when we have no prior mode.
        self.assertEqual(self._sub(4000 + HYST_W // 2, None), "discharge")

    def test_exactly_at_target_is_the_hold_band_not_discharge(self):
        self.assertEqual(self._sub(4000, "bank"), "bank")

    def test_one_watt_below_the_bank_threshold_is_still_the_hold_band(self):
        self.assertEqual(self._sub(4000 + HYST_W - 1, "bank"), "bank")

    def test_surplus_never_goes_negative(self):
        c = _ctrl(export_target_kw=4.0)
        st = c.drive(_snap(pv=500, home=3000))
        self.assertEqual(st["surplus_w"], 0)
        self.assertEqual(st["submode"], "discharge")


class TestDischargeEntry(unittest.TestCase):
    def test_daylight_uses_pv_first(self):
        mb = FakeModbus()
        c = _ctrl(mb, export_target_kw=4.0, inverter_max_kw=10.0)
        c.drive(_snap(pv=DAYTIME_PV_W + 1, home=0))
        self.assertIn(("daytime_export", 10000), mb.calls)
        self.assertNotIn("night_export", mb.names())

    def test_darkness_uses_ess_first(self):
        mb = FakeModbus()
        c = _ctrl(mb, export_target_kw=4.0, inverter_max_kw=10.0)
        c.drive(_snap(pv=DAYTIME_PV_W, home=0))     # boundary: NOT daytime
        self.assertIn(("night_export", 10000), mb.calls)
        self.assertNotIn("daytime_export", mb.names())

    def test_the_mode_is_written_once_not_every_tick(self):
        # The daemon ticks every 5s; re-writing the mode register every tick is
        # both pointless Modbus traffic and a log line a second.
        mb = FakeModbus()
        c = _ctrl(mb, export_target_kw=4.0)
        for _ in range(5):
            c.drive(_snap(pv=100, home=0))
        self.assertEqual(mb.names().count("night_export"), 1)

    def test_re_entering_discharge_writes_again(self):
        mb = FakeModbus()
        c = _ctrl(mb, export_target_kw=4.0)
        c.drive(_snap(pv=100, home=0))                 # discharge
        c.drive(_snap(pv=4000 + HYST_W, home=0))       # bank
        c.drive(_snap(pv=100, home=0))                 # discharge again
        self.assertEqual(mb.names().count("night_export"), 2)


class TestBankEntry(unittest.TestCase):
    def test_entering_bank_resets_to_self_consumption_and_caps_the_charge(self):
        mb = FakeModbus()
        c = _ctrl(mb, export_target_kw=4.0)
        c.drive(_snap(pv=6000, home=0))
        self.assertEqual(mb.names()[0], "self_consumption")
        self.assertIn(("charge_limit", 2000), mb.calls)   # 6000 - 4000 target

    def test_self_consumption_is_written_once_per_entry(self):
        mb = FakeModbus()
        c = _ctrl(mb, export_target_kw=4.0)
        for _ in range(4):
            c.drive(_snap(pv=6000, home=0))
        self.assertEqual(mb.names().count("self_consumption"), 1)

    def test_the_charge_cap_is_only_rewritten_past_the_deadband(self):
        mb = FakeModbus()
        c = _ctrl(mb, export_target_kw=4.0)
        c.drive(_snap(pv=6000, home=0))                      # cap 2000, written
        before = mb.names().count("charge_limit")
        c.drive(_snap(pv=6000 + BANK_CAP_DEADBAND, home=0))  # +300, NOT past it
        self.assertEqual(mb.names().count("charge_limit"), before)
        c.drive(_snap(pv=6000 + BANK_CAP_DEADBAND + 1, home=0))   # past it
        self.assertEqual(mb.names().count("charge_limit"), before + 1)

    def test_the_cap_is_the_surplus_above_target_not_the_surplus(self):
        # Getting this wrong banks the export itself and sends nothing to the grid.
        mb = FakeModbus()
        c = _ctrl(mb, export_target_kw=4.0)
        c.drive(_snap(pv=9000, home=1000))    # surplus 8000, target 4000
        self.assertIn(("charge_limit", 4000), mb.calls)

    def test_reported_bank_cap_is_none_while_discharging(self):
        c = _ctrl(export_target_kw=4.0)
        self.assertIsNone(c.drive(_snap(pv=100, home=0))["bank_cap_w"])


class TestPrecharge(unittest.TestCase):
    def test_the_reserve_floor_is_raised(self):
        mb = FakeModbus()
        c = _ctrl(mb, reserve_floor_pct=25.0, battery_capacity_kwh=0)
        c.precharge(_snap(soc=80))
        self.assertIn(("discharge_cutoff", 25.0), mb.calls)

    def test_a_short_battery_warns_but_still_proceeds(self):
        # Never imports from grid to top up — it says so and carries on.
        mb = FakeModbus()
        c = _ctrl(mb, reserve_floor_pct=10.0, battery_capacity_kwh=10.0,
                  export_target_kw=4.0)
        with self.assertLogs("test", level="WARNING") as cm:
            c.precharge(_snap(soc=15))     # (15-10)% of 10 kWh = 0.5 kWh vs 4.4 needed
        self.assertTrue(any("Pre-charge" in m for m in cm.output))
        self.assertIn(("discharge_cutoff", 10.0), mb.calls)

    def test_a_full_battery_does_not_warn(self):
        c = _ctrl(reserve_floor_pct=10.0, battery_capacity_kwh=35.0,
                  export_target_kw=4.0)
        with self.assertLogs("test", level="INFO") as cm:
            c.precharge(_snap(soc=90))
        self.assertFalse(any(r.levelno >= 30 for r in cm.records))

    def test_capacity_zero_skips_the_sufficiency_check(self):
        c = _ctrl(reserve_floor_pct=10.0, battery_capacity_kwh=0)
        with self.assertLogs("test", level="INFO") as cm:
            c.precharge(_snap(soc=5))     # would be hopeless if it were checked
        self.assertFalse(any(r.levelno >= 30 for r in cm.records))

    def test_a_missing_soc_does_not_explode(self):
        # read_all() can hand back a snapshot with the key absent or None.
        c = _ctrl(reserve_floor_pct=10.0, battery_capacity_kwh=10.0)
        for snap in ({}, {"batterySoc": None}):
            with self.subTest(snap=snap):
                c.precharge(snap)


class TestRestore(unittest.TestCase):
    """The safety net. The daemon calls this from a finally and from atexit, so a
    crash must never leave the battery forced into export."""

    def test_restores_self_consumption(self):
        mb = FakeModbus()
        c = _ctrl(mb)
        c.restore()
        self.assertIn(("self_consumption",), mb.calls)

    def test_the_cutoff_is_only_dropped_if_precharge_raised_it(self):
        mb = FakeModbus()
        c = _ctrl(mb, health_floor_pct=1.0)
        c.restore()
        self.assertNotIn("discharge_cutoff", mb.names())

    def test_after_precharge_the_cutoff_returns_to_the_health_floor(self):
        mb = FakeModbus()
        c = _ctrl(mb, reserve_floor_pct=25.0, health_floor_pct=1.0,
                  battery_capacity_kwh=0)
        c.precharge(_snap(soc=80))
        c.restore()
        self.assertEqual(mb.calls[-1], ("discharge_cutoff", 1.0))

    def test_is_safe_to_call_twice(self):
        mb = FakeModbus()
        c = _ctrl(mb, battery_capacity_kwh=0)
        c.precharge(_snap(soc=80))
        c.restore()
        c.restore()
        self.assertEqual(mb.names().count("discharge_cutoff"), 2)   # raise, then one drop

    def test_state_is_cleared_even_when_modbus_throws(self):
        # The finally exists precisely so a failed write does not leave the
        # controller believing it is still mid-event. The exception still escapes,
        # because swallowing it would hide an inverter that is stuck exporting.
        class Broken(FakeModbus):
            def set_self_consumption(self):
                raise OSError("connection reset")

        c = _ctrl(Broken(), export_target_kw=4.0)
        c.drive(_snap(pv=100, home=0))
        self.assertEqual(c._submode, "discharge")
        with self.assertRaises(OSError):
            c.restore()
        self.assertIsNone(c._submode)
        self.assertEqual(c._bank_cap_w, -1)
        self.assertFalse(c._cutoff_raised)

    def test_a_restored_controller_re_enters_cleanly(self):
        mb = FakeModbus()
        c = _ctrl(mb, export_target_kw=4.0)
        c.drive(_snap(pv=100, home=0))
        c.restore()
        c.drive(_snap(pv=100, home=0))
        self.assertEqual(mb.names().count("night_export"), 2)


class TestStatusDict(unittest.TestCase):
    def test_carries_the_live_figures_through(self):
        c = _ctrl(export_target_kw=4.0)
        st = c.drive(_snap(pv=5000, home=1200, grid=-3800, battery=-100, soc=88.5))
        self.assertEqual(st["pv_w"], 5000)
        self.assertEqual(st["home_w"], 1200)
        self.assertEqual(st["grid_w"], -3800)
        self.assertEqual(st["battery_w"], -100)
        self.assertEqual(st["soc_pct"], 88.5)
        self.assertEqual(st["surplus_w"], 3800)
        self.assertEqual(st["target_w"], 4000)

    def test_missing_snapshot_keys_read_as_zero_not_a_crash(self):
        c = _ctrl(export_target_kw=4.0)
        st = c.drive({})
        self.assertEqual((st["pv_w"], st["home_w"], st["grid_w"]), (0, 0, 0))

    def test_none_values_read_as_zero(self):
        # read_all() uses None for a register it could not read, and `or 0` is what
        # catches it — a bare .get(k, 0) would hand None into int().
        c = _ctrl(export_target_kw=4.0)
        st = c.drive({"pvPowerWatts": None, "homePowerWatts": None,
                      "gridPowerWatts": None, "batteryPowerWatts": None})
        self.assertEqual(st["pv_w"], 0)


if __name__ == "__main__":
    unittest.main()
