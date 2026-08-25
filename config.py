#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    config.py
# Description: Load / save / default the SigenVPP standalone daemon config (JSON).
#              No Indigo, no Claude — plain Python, runs anywhere.
# Author:      CliveS & Claude Opus 4.8
# Date:        15-06-2026
# Version:     0.1

import json
import os

CONFIG_FILENAME = "config.json"

# Defaults. The setup wizard fills inverter.ip + vpp.axle_token (the only two the
# user must supply) and auto-detects vpp.export_target_kw from the inverter where
# it can. Everything else has a sensible default and is rarely touched.
DEFAULTS = {
    "inverter": {
        "ip":               "",      # REQUIRED — your Sigenergy's LAN IP
        "port":             502,     # Modbus TCP (Sigen default)
        "plant_address":    247,     # plant slave (Sigen default)
        "inverter_slave_id": 1,      # inverter slave (Sigen default)
    },
    "vpp": {
        "axle_token":           "",      # REQUIRED — Bearer token from your Axle account
        "export_target_kw":     4.0,     # grid export target = your DNO/G99 cap (auto-read where possible)
        "inverter_max_kw":      10.0,    # inverter rated AC power
        "battery_capacity_kwh": 0.0,     # total usable battery (for pre-charge sizing; 0 = skip the SOC check)
        "reserve_floor_pct":    10.0,    # don't let the battery discharge below this during an event
        "health_floor_pct":     1.0,     # the floor restored after an event
        "precharge_lead_min":   30,      # raise the reserve floor this long before the event
        "start_lead_min":       2,       # begin driving the export this long before the start
        "end_trail_min":        2,       # keep driving this long past the end, then restore
        "poll_interval_min":    10,      # how often to poll Axle for the next event
        "poll_modbus_s":        5,       # how often to read the inverter (live dashboard cadence)
        "rate_per_kwh":         1.00,    # Axle pay rate — display/estimate only
    },
    "notify": {
        "pushover_token": "",   # optional — Pushover application API token
        "pushover_user":  "",   # optional — Pushover user/group key
    },
    "web": {
        "enabled": True,
        "bind":    "127.0.0.1",  # this machine only. "0.0.0.0" opens it to the whole
                                # LAN with no auth - deliberate choice, not a default
        "port":    8179,
    },
}


def _deep_merge(base, override):
    """Return base updated with override, recursively (override wins)."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path(directory=None):
    """Absolute path to config.json next to this script (or in `directory`)."""
    base = directory or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CONFIG_FILENAME)


def load_config(path=None):
    """Load config.json merged over DEFAULTS. Returns (config_dict, exists_bool)."""
    path = path or config_path()
    if not os.path.exists(path):
        return _deep_merge(DEFAULTS, {}), False
    with open(path, encoding="utf-8") as fh:
        user = json.load(fh)
    return _deep_merge(DEFAULTS, user), True


def save_config(cfg, path=None):
    """Write config.json (pretty) and lock it to owner-only (0600) — it holds the
    Axle token and any Pushover keys."""
    path = path or config_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=False)
        fh.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass   # best-effort (e.g. on a filesystem that ignores chmod)
    return path


def is_configured(cfg):
    """True once the two required fields are present."""
    return bool(cfg["inverter"]["ip"]) and bool(cfg["vpp"]["axle_token"])
