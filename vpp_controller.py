#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    vpp_controller.py
# Description: Standalone VPP export controller for Sigenergy — the bank/discharge
#              driver lifted out of the Indigo plugin (SigenEnergyManager v5.30.1),
#              with no Indigo dependency. Drives one inverter over Modbus to export
#              the DNO-capped target during an Axle VPP window: banks PV surplus when
#              the sun's strong, discharges the battery to top up when it isn't, and
#              always restores Max Self Consumption afterwards.
# Author:      CliveS & Claude Opus 4.8
# Date:        15-06-2026
# Version:     0.1

import logging

# Decide bank vs discharge with a one-sided hysteresis around the export target so a
# brief PV spike can't flap us into self-consumption, while a drop below target always
# falls back to discharge so the full export is guaranteed (the 5.30.1 fix).
HYST_W            = 400
DAYTIME_PV_W      = 300    # live PV above this => treat as a daylight window
BANK_CAP_DEADBAND = 300    # only re-write the charge cap when it shifts more than this


class VppController:
    """Drives a single Sigenergy through a VPP export window. Stateless across
    restarts except for the small in-memory sub-mode latch; call restore() (or let
    the caller's finally/atexit do it) to return the inverter to a safe baseline."""

    def __init__(self, modbus, config, logger=None):
        self.modbus = modbus
        self.cfg    = config
        self.log    = logger or logging.getLogger("SigenVPP.Controller")
        self._submode    = None    # "bank" | "discharge" | None
        self._bank_cap_w = -1
        self._cutoff_raised = False

    # --- config-derived watts ---
    @property
    def target_w(self):
        return int(float(self.cfg["vpp"]["export_target_kw"]) * 1000)

    @property
    def inv_max_w(self):
        return int(float(self.cfg["vpp"]["inverter_max_kw"]) * 1000)

    # --- pre-charge / reserve protection ---
    def precharge(self, snapshot):
        """Raise the discharge cutoff to the reserve floor so an event can't drain the
        battery below the user's reserve. Logs a warning if SOC looks short, but never
        imports from grid to make it up (solar/own choice fills it)."""
        reserve = float(self.cfg["vpp"]["reserve_floor_pct"])
        self.modbus.set_discharge_cutoff(reserve)
        self._cutoff_raised = True

        cap_kwh = float(self.cfg["vpp"].get("battery_capacity_kwh", 0) or 0)
        soc     = float(snapshot.get("batterySoc", 0) or 0)
        if cap_kwh > 0:
            target_kwh = float(self.cfg["vpp"]["export_target_kw"]) * 1.1  # ~1h + losses
            have_kwh   = max(0.0, (soc - reserve) / 100.0 * cap_kwh)
            if have_kwh < target_kwh:
                self.log.warning(
                    "Pre-charge: SOC %.0f%% gives ~%.1f kWh above the %.0f%% reserve, "
                    "less than the ~%.1f kWh the event may need. Proceeding without grid "
                    "import — solar/your own charging fills the gap.",
                    soc, have_kwh, reserve, target_kwh)
            else:
                self.log.info("Pre-charge OK: SOC %.0f%%, reserve floor set to %.0f%%.",
                              soc, reserve)
        else:
            self.log.info("Pre-charge: reserve floor set to %.0f%% "
                          "(battery_capacity_kwh not set — SOC sufficiency not checked).",
                          reserve)

    # --- the per-tick driver (port of plugin _drive_vpp_export, v5.30.1) ---
    def drive(self, snapshot):
        """One control step against a live read_all() snapshot. Picks the sub-mode,
        writes Modbus only on a change, and returns a status dict for logging/UI."""
        target  = self.target_w
        inv_max = self.inv_max_w
        pv_w    = int(snapshot.get("pvPowerWatts", 0) or 0)
        home_w  = int(snapshot.get("homePowerWatts", 0) or 0)
        surplus = max(0, pv_w - home_w)
        daytime = pv_w > DAYTIME_PV_W
        prev    = self._submode

        # Guarantee the export: discharge the moment surplus < target; only ENTER bank
        # when comfortably above target; hold the current mode in the [target,+HYST) band.
        if surplus < target:
            sub = "discharge"
        elif surplus >= target + HYST_W:
            sub = "bank"
        else:
            sub = prev or "discharge"

        if sub == "bank":
            cap = max(0, surplus - target)
            if prev != "bank":
                self.modbus.set_self_consumption()      # mode 0x02, limits reset to max
                self._bank_cap_w = -1
                self.log.info("Bank-surplus export — PV %dW home %dW surplus %dW >= "
                              "target %dW: mode 0x02, charge cap %dW (export target from "
                              "PV, bank the rest).", pv_w, home_w, surplus, target, cap)
            if abs(self._bank_cap_w - cap) > BANK_CAP_DEADBAND:
                self.modbus.set_charge_limit(cap, quiet=True)
                self._bank_cap_w = cap
        else:
            if prev != "discharge":
                if daytime:
                    self.modbus.daytime_export(inv_max)   # mode 0x05 + charge 0
                    mode = "0x05 PV-first"
                else:
                    self.modbus.night_export(inv_max)     # mode 0x06
                    mode = "0x06 ESS-first"
                self._bank_cap_w = -1
                self.log.info("Discharge export — PV %dW home %dW surplus %dW < target "
                              "%dW: mode %s, battery tops the export up to %dW.",
                              pv_w, home_w, surplus, target, mode, target)

        self._submode = sub
        return {
            "submode":    sub,
            "pv_w":       pv_w,
            "home_w":     home_w,
            "grid_w":     int(snapshot.get("gridPowerWatts", 0) or 0),
            "battery_w":  int(snapshot.get("batteryPowerWatts", 0) or 0),
            "soc_pct":    snapshot.get("batterySoc"),
            "surplus_w":  surplus,
            "target_w":   target,
            "bank_cap_w": self._bank_cap_w if sub == "bank" else None,
        }

    # --- restore to a safe baseline ---
    def restore(self):
        """Return the inverter to Max Self Consumption and drop the discharge cutoff
        back to the health floor. Safe to call repeatedly; the daemon calls it from a
        finally / signal handler so a crash never leaves the battery forced."""
        try:
            self.modbus.set_self_consumption()
            if self._cutoff_raised:
                self.modbus.set_discharge_cutoff(float(self.cfg["vpp"]["health_floor_pct"]))
            self.log.info("Restored: Max Self Consumption, discharge cutoff at health floor.")
        finally:
            self._submode = None
            self._bank_cap_w = -1
            self._cutoff_raised = False
