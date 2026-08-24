#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    sigen_vpp.py
# Description: SigenVPP — standalone Axle VPP export controller for Sigenergy.
#              No Indigo, no Claude, no cloud (except reading the Axle announcement).
#              Plain Python over Modbus TCP; runs on macOS / Windows / Linux / Pi / NAS.
#
#              Subcommands:
#                --setup         interactive first-run wizard (writes config.json)
#                --status        one-shot: live inverter snapshot + next Axle event
#                --test-export   drive the export for N minutes then restore (PLUGIN OFF)
#                --run           the daemon: poll Axle, drive each announced window
#
# Author:      CliveS & Claude Opus 4.8
# Date:        17-07-2026
# Version:     0.2
#
# Changes: Local-clock display moved from pytz to stdlib zoneinfo — the daemon now
#          depends only on pymodbus + requests (no optional pytz).

import argparse
import atexit
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import config as cfgmod
from axle_api import AxleAPI

try:
    from sigenergy_modbus import SigenergyModbus, PYMODBUS_AVAILABLE
except Exception:
    SigenergyModbus, PYMODBUS_AVAILABLE = None, False

log = logging.getLogger("SigenVPP")

# Module-level handle so atexit / signal handlers can always restore the inverter.
_ACTIVE = {"controller": None, "modbus": None, "driving": False}


class DaemonState:
    """Thread-safe live status the web dashboard reads via snapshot()."""

    def __init__(self):
        self._lock = threading.Lock()
        self._d = {
            "ok": True, "modbus_connected": False, "phase": "starting",
            "submode": None, "inverter": {}, "target_w": None,
            "bank_cap_w": None, "next_event": None, "axle_ok": False, "updated": None,
        }

    def update(self, **kw):
        with self._lock:
            self._d.update(kw)

    def set_inverter(self, snap):
        inv = {
            "pv_w":      snap.get("pvPowerWatts"),
            "home_w":    snap.get("homePowerWatts"),
            "grid_w":    snap.get("gridPowerWatts"),
            "battery_w": snap.get("batteryPowerWatts"),
            "soc_pct":   snap.get("batterySoc"),
            "ems":       snap.get("emsWorkMode"),
        }
        with self._lock:
            self._d["inverter"] = inv
            self._d["updated"]  = datetime.now().strftime("%H:%M:%S")

    def snapshot(self):
        with self._lock:
            return dict(self._d)


def _fmt_local(start, end):
    """Human local-clock string for an event, e.g. '15 Jun 07:00–08:00'."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/London")
        s, e = start.astimezone(tz), end.astimezone(tz)
        return f"{s:%d %b %H:%M}–{e:%H:%M}"
    except Exception:
        return f"{start:%d %b %H:%M}–{end:%H:%M} UTC"


# ============================================================
# Helpers
# ============================================================

def _setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _connect_modbus(cfg):
    """Return a connected SigenergyModbus, or None (with a clear message)."""
    if not PYMODBUS_AVAILABLE or SigenergyModbus is None:
        log.error("pymodbus is not installed — run:  pip install pymodbus requests")
        return None
    inv = cfg["inverter"]
    mb = SigenergyModbus(
        inv["ip"], port=int(inv["port"]),
        plant_address=int(inv["plant_address"]),
        inverter_address=int(inv["inverter_slave_id"]),
        logger=logging.getLogger("SigenVPP.Modbus"),
    )
    if not mb.connect():
        log.error("Could not connect to the inverter at %s:%s — check the IP and that "
                  "Modbus TCP is enabled on the Sigenergy.", inv["ip"], inv["port"])
        return None
    return mb


def _pushover(cfg, title, body):
    """Best-effort Pushover notification over plain HTTPS (no Indigo)."""
    tok = cfg["notify"].get("pushover_token", "")
    usr = cfg["notify"].get("pushover_user", "")
    if not tok or not usr:
        return
    try:
        import requests
        requests.post("https://api.pushover.net/1/messages.json", timeout=10, data={
            "token": tok, "user": usr, "title": title, "message": body,
        })
    except Exception as exc:
        log.warning("Pushover send failed: %s", exc)


def _restore_on_exit():
    """atexit/signal hook — never leave the battery forced if we were driving."""
    if _ACTIVE["driving"] and _ACTIVE["controller"]:
        log.warning("Exiting while driving — restoring Max Self Consumption first.")
        try:
            _ACTIVE["controller"].restore()
        except Exception as exc:
            log.error("Restore on exit failed: %s", exc)
    if _ACTIVE["modbus"]:
        try:
            _ACTIVE["modbus"].disconnect()
        except Exception:
            pass


def _install_signal_handlers():
    atexit.register(_restore_on_exit)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_a: sys.exit(0))   # triggers atexit
        except (ValueError, OSError):
            pass   # not in main thread / unsupported platform


def _ask(prompt, default=None):
    suffix = f" [{default}]" if default not in (None, "") else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or (str(default) if default is not None else "")


# ============================================================
# --setup wizard
# ============================================================

def cmd_setup(args):
    cfg, existed = cfgmod.load_config()
    print("\n=== SigenVPP setup ===")
    print("Press Enter to accept the [default]. Two things are essential: the inverter's")
    print("IP address and your Axle API token. The rest is auto-detected or defaulted.\n")

    # 1. Inverter IP + connectivity test
    ip = _ask("Sigenergy inverter IP address", cfg["inverter"]["ip"] or None)
    cfg["inverter"]["ip"] = ip
    port = _ask("Modbus TCP port", cfg["inverter"]["port"])
    cfg["inverter"]["port"] = int(port)

    detected_export_kw = None
    if PYMODBUS_AVAILABLE and ip:
        print(f"\nTesting connection to {ip}:{port} ...")
        mb = _connect_modbus(cfg)
        if mb:
            snap = mb.read_all() or {}
            soc = snap.get("batterySoc")
            pv  = snap.get("pvPowerWatts")
            print(f"  Connected. Live: SOC {soc}% , PV {pv} W.")
            try:
                exp_w = mb.read_export_limit()
                if exp_w:
                    detected_export_kw = round(exp_w / 1000.0, 2)
                    print(f"  Detected commissioned export cap: {detected_export_kw} kW "
                          "(your DNO/G99 limit).")
            except Exception:
                pass
            mb.disconnect()
        else:
            print("  Could not connect — you can still finish setup and fix the IP later.")
    else:
        print("\n(pymodbus not available or IP blank — skipping the live connection test.)")

    # 2. Axle token
    print("\nYour Axle API token is the Bearer token from your Axle VPP account")
    print("(the same one Axle's Home Assistant integration uses).")
    cfg["vpp"]["axle_token"] = _ask("Axle API token", cfg["vpp"]["axle_token"] or None)

    # 3. Export target (pre-filled from the inverter where detected)
    default_export = detected_export_kw or cfg["vpp"]["export_target_kw"]
    cfg["vpp"]["export_target_kw"] = float(_ask("Grid export target (kW)", default_export))

    # 4. Battery + reserve
    cfg["vpp"]["battery_capacity_kwh"] = float(
        _ask("Battery usable capacity (kWh, 0 to skip the SOC check)",
             cfg["vpp"]["battery_capacity_kwh"]))
    cfg["vpp"]["reserve_floor_pct"] = float(
        _ask("Reserve floor (%) — don't discharge below this during an event",
             cfg["vpp"]["reserve_floor_pct"]))
    cfg["vpp"]["inverter_max_kw"] = float(
        _ask("Inverter rated power (kW)", cfg["vpp"]["inverter_max_kw"]))

    # 5. Optional Pushover
    if _ask("Add Pushover notifications? (y/N)", "N").lower().startswith("y"):
        cfg["notify"]["pushover_token"] = _ask("  Pushover application token",
                                               cfg["notify"]["pushover_token"] or None)
        cfg["notify"]["pushover_user"] = _ask("  Pushover user/group key",
                                              cfg["notify"]["pushover_user"] or None)

    path = cfgmod.save_config(cfg)
    print(f"\nSaved {path} (locked to owner-only).")
    print("Validate it any time with:  python3 sigen_vpp.py --status")
    if not cfgmod.is_configured(cfg):
        print("NOTE: inverter IP and/or Axle token still blank — re-run --setup to finish.")
    return 0


# ============================================================
# --status (read-only; safe alongside a running plugin only for the Axle half)
# ============================================================

def cmd_status(args):
    cfg, existed = cfgmod.load_config()
    if not existed:
        print("No config.json yet — run:  python3 sigen_vpp.py --setup")
        return 1

    # Axle half (no inverter writes; safe anywhere)
    print("\n--- Axle ---")
    event = AxleAPI(cfg["vpp"]["axle_token"]).get_next_event()
    if event:
        try:
            from zoneinfo import ZoneInfo
            _tz = ZoneInfo("Europe/London")
            s = event["start_time"].astimezone(_tz)
            e = event["end_time"].astimezone(_tz)
            when = f"{s:%d %b %H:%M} - {e:%H:%M} (local)"
        except Exception:
            when = f"{event['start_time']:%Y-%m-%d %H:%M} - {event['end_time']:%H:%M} UTC"
        print(f"  Next event: {event['import_export']}  {when}  "
              f"({event['duration_hrs']:.1f}h)")
    else:
        print("  No event scheduled.")

    # Inverter half (read-only). NB: a second Modbus client may contend with the
    # Indigo plugin if it's running — only run this with the plugin disabled.
    print("\n--- Inverter ---")
    mb = _connect_modbus(cfg)
    if mb:
        snap = mb.read_all() or {}
        for k in ("batterySoc", "pvPowerWatts", "homePowerWatts",
                  "batteryPowerWatts", "gridPowerWatts", "emsWorkMode"):
            print(f"  {k}: {snap.get(k)}")
        mb.disconnect()
    return 0


# ============================================================
# --test-export (Tier 2 — RUN WITH THE INDIGO PLUGIN DISABLED)
# ============================================================

def cmd_test_export(args):
    cfg, existed = cfgmod.load_config()
    if not existed:
        print("No config.json — run --setup first.")
        return 1
    from vpp_controller import VppController

    mb = _connect_modbus(cfg)
    if not mb:
        return 1
    _ACTIVE["modbus"] = mb
    ctrl = VppController(mb, cfg, logging.getLogger("SigenVPP.Controller"))
    _ACTIVE["controller"] = ctrl
    _install_signal_handlers()

    minutes = args.minutes
    log.warning("TEST EXPORT for %d min. Ensure the Indigo plugin is DISABLED so the "
                "two don't fight over the inverter. Ctrl-C restores at any time.", minutes)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    _ACTIVE["driving"] = True
    try:
        while datetime.now(timezone.utc) < deadline:
            snap = mb.read_all() or {}
            status = ctrl.drive(snap)
            log.info("drive: %s", status)
            time.sleep(15)
    finally:
        _ACTIVE["driving"] = False
        ctrl.restore()
        mb.disconnect()
    log.info("Test complete, inverter restored. Re-enable the Indigo plugin.")
    return 0


# ============================================================
# --run (the daemon)
# ============================================================

def cmd_run(args):
    cfg, existed = cfgmod.load_config()
    if not existed or not cfgmod.is_configured(cfg):
        print("Not configured — run:  python3 sigen_vpp.py --setup")
        return 1
    from vpp_controller import VppController
    from web_dashboard import WebDashboard

    state  = DaemonState()
    axle   = AxleAPI(cfg["vpp"]["axle_token"])
    fast_s = int(cfg["vpp"].get("poll_modbus_s", 5))
    poll_s = int(cfg["vpp"]["poll_interval_min"]) * 60
    lead   = int(cfg["vpp"]["start_lead_min"])
    trail  = int(cfg["vpp"]["end_trail_min"])
    pre    = int(cfg["vpp"]["precharge_lead_min"])
    target_w = int(float(cfg["vpp"]["export_target_kw"]) * 1000)
    state.update(target_w=target_w)

    web = None
    if cfg["web"].get("enabled", True):
        web = WebDashboard(state.snapshot, host=cfg["web"]["bind"],
                           port=int(cfg["web"]["port"]),
                           logger=logging.getLogger("SigenVPP.Web"))
        web.start()
    _install_signal_handlers()
    log.warning("SigenVPP daemon started. ONE controller per inverter — the Indigo plugin "
                "must NOT also be driving this Sigenergy.")

    mb = ctrl = None
    event = None
    last_axle = 0.0
    precharged_for = None
    reconciled = False
    phase = "idle"

    while True:
        # 1. Ensure a live Modbus connection
        if mb is None or not mb.connected:
            mb = _connect_modbus(cfg)
            if mb is None:
                state.update(modbus_connected=False, phase="offline")
                time.sleep(fast_s)
                continue
            ctrl = VppController(mb, cfg, logging.getLogger("SigenVPP.Controller"))
            _ACTIVE["modbus"], _ACTIVE["controller"] = mb, ctrl
            reconciled = False

        snap = mb.read_all()
        if snap is None:
            state.update(modbus_connected=False)
            mb.disconnect()
            mb = None
            time.sleep(fast_s)
            continue
        state.update(modbus_connected=True)
        state.set_inverter(snap)

        # 2. Refresh the next event on the slow cadence
        if last_axle == 0.0 or (time.time() - last_axle) >= poll_s:
            ev = axle.get_next_event()
            last_axle = time.time()
            state.update(axle_ok=True)
            event = ev if (ev and ev.get("import_export") == "export") else None

        # 3. Work out where we are relative to the window
        now = datetime.now(timezone.utc)
        ev_state = None
        in_window = in_precharge = False
        if event:
            start, end = event["start_time"], event["end_time"]
            drive_from  = start - timedelta(minutes=lead)
            drive_until = end + timedelta(minutes=trail)
            precharge_at = start - timedelta(minutes=pre)
            in_window    = drive_from <= now < drive_until
            in_precharge = precharge_at <= now < drive_from
            ev_state = {
                "start": start.isoformat(), "end": end.isoformat(),
                "local": _fmt_local(start, end),
                "duration_hrs": round(event["duration_hrs"], 2),
                "starts_in_s": max(0, (drive_from - now).total_seconds()),
                "active": in_window,
            }
        state.update(next_event=ev_state)

        # 4. Act
        if event and in_window:
            st = ctrl.drive(snap)
            _ACTIVE["driving"] = True
            phase = "active"
            state.update(phase="active", submode=st["submode"],
                         bank_cap_w=st.get("bank_cap_w"))
        elif event and in_precharge:
            if precharged_for != event["start_time"].isoformat():
                ctrl.precharge(snap)
                precharged_for = event["start_time"].isoformat()
                _pushover(cfg, "VPP event upcoming",
                          f"{_fmt_local(event['start_time'], event['end_time'])} — "
                          "reserve floor raised, ready to export.")
            _ACTIVE["driving"] = False
            phase = "precharging"
            state.update(phase="precharging", submode=None, bank_cap_w=None)
        else:
            # Not in a window. Restore once on a just-ended event, and once at startup
            # (dead-man reconcile in case a prior crash left the inverter forced).
            if phase == "active":
                _ACTIVE["driving"] = False
                ctrl.restore()
                _pushover(cfg, "VPP event complete", "Export window finished, restored.")
            elif not reconciled:
                ctrl.restore()
            reconciled = True
            phase = "idle"
            state.update(phase="announced" if event else "idle",
                         submode=None, bank_cap_w=None)

        time.sleep(fast_s)


# ============================================================
# Entry
# ============================================================

def main():
    p = argparse.ArgumentParser(description="SigenVPP — standalone Axle VPP export for Sigenergy")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--setup", action="store_true", help="interactive first-run wizard")
    g.add_argument("--status", action="store_true", help="show next event + live inverter")
    g.add_argument("--test-export", action="store_true",
                   help="drive the export for N minutes then restore (plugin OFF)")
    g.add_argument("--run", action="store_true", help="run the daemon")
    p.add_argument("--minutes", type=int, default=3, help="--test-export duration (default 3)")
    args = p.parse_args()

    _setup_logging(args.verbose)
    if args.setup:
        return cmd_setup(args)
    if args.status:
        return cmd_status(args)
    if getattr(args, "test_export"):
        return cmd_test_export(args)
    if args.run:
        return cmd_run(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
