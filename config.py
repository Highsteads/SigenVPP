#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    config.py
# Description: Load / save / default the SigenVPP standalone daemon config (JSON).
#              No Indigo, no Claude — plain Python, runs anywhere.
# Author:      CliveS & Claude Opus 4.8
# Date:        15-06-2026
# Version:     0.2
#
# v0.2 (20-09-2026) — two ways a config could look fine and not work:
#   * a token pasted with a trailing newline passed is_configured() and then got
#     a 401 from Axle. Trimming is done ON LOAD, not at the check, because
#     trimming only at the check would make setup say yes while the daemon still
#     sent the untrimmed value.
#   * a corrupt config.json raised a bare JSONDecodeError out of load_config()
#     naming no file. It now raises ConfigError with the path, the line and
#     column, and what to do about it.

import json
import os

CONFIG_FILENAME = "config.json"


class ConfigError(Exception):
    """A config.json that exists but cannot be used.

    Carries the path and a plain-English reason so the daemon prints one line
    rather than a traceback. Deliberately NOT a fall back to defaults: running
    on DEFAULTS after failing to read the user's file would drive the inverter
    to a 4 kW export target and a 10% reserve that nobody chose, and the only
    sign would be a log line nobody reads. Refusing to start is the safe answer.
    """


# Values people paste by hand, which routinely arrive with a trailing newline or
# a leading space when copied out of a browser or a password manager.
_TRIM_FIELDS = (
    ("inverter", "ip"),
    ("vpp",      "axle_token"),
    ("notify",   "pushover_token"),
    ("notify",   "pushover_user"),
)

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


def _trim_pasted_fields(cfg):
    """Strip surrounding whitespace from the hand-pasted values, in place.

    Done on LOAD so every consumer sees the clean value. Doing it in
    is_configured() instead would fix only the check: setup would report the
    daemon ready and Axle would still answer 401 for the untrimmed token.
    """
    for section, key in _TRIM_FIELDS:
        sect = cfg.get(section)
        if isinstance(sect, dict) and isinstance(sect.get(key), str):
            sect[key] = sect[key].strip()
    return cfg


def config_path(directory=None):
    """Absolute path to config.json next to this script (or in `directory`)."""
    base = directory or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CONFIG_FILENAME)


def load_config(path=None):
    """Load config.json merged over DEFAULTS. Returns (config_dict, exists_bool).

    A MISSING file is normal — that is the (defaults, False) case the wizard
    starts from. A file that exists and cannot be used raises ConfigError, which
    names the path and says what to do; see that class for why this never falls
    back to defaults.
    """
    path = path or config_path()
    if not os.path.exists(path):
        return _trim_pasted_fields(_deep_merge(DEFAULTS, {})), False

    try:
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON — {exc.msg}, line {exc.lineno} column {exc.colno}. "
            f"Fix it by hand, or delete it and run:  python3 sigen_vpp.py --setup"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{path} is not readable as UTF-8 text ({exc.reason}). "
            f"Delete it and run:  python3 sigen_vpp.py --setup"
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"{path} could not be read — {exc.strerror}."
        ) from exc

    # Valid JSON is not necessarily a config: a bare list or string would reach
    # _deep_merge and die on .items() with a message about neither the file nor
    # the problem.
    if not isinstance(user, dict):
        raise ConfigError(
            f"{path} must hold a JSON object, not a {type(user).__name__}. "
            f"Delete it and run:  python3 sigen_vpp.py --setup"
        )

    return _trim_pasted_fields(_deep_merge(DEFAULTS, user)), True


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
    """True once the two required fields hold something other than whitespace.

    load_config() has already trimmed them; this strips again so a config built
    by hand in a test or a script gets the same answer, and tolerates a section
    that a hand-edited file replaced with a scalar.
    """
    def _filled(section, key):
        sect = cfg.get(section)
        if not isinstance(sect, dict):
            return False
        return bool(str(sect.get(key) or "").strip())

    return _filled("inverter", "ip") and _filled("vpp", "axle_token")
