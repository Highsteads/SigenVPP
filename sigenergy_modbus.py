#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    sigenergy_modbus.py
# Description: Sigenergy inverter Modbus TCP client - reads all registers
#              and controls battery via Remote EMS
# Author:      CliveS & Claude Opus 5
# Date:        24-08-2026
# Version:     1.12 (read_export_limit() — reads back the commissioned grid
#              export cap 40038; the write side already existed)
#              prior 1.11 (REAL grid voltage 31011 + current 31017, rated capacity
#              30548 — the nameplate/measurement distinction again)
#              prior 1.10 (PCS internal temp, insulation resistance, PACK count and
#              alarm word — all NAMED from the official V2.7 protocol PDF)
#              prior 1.9 (grid frequency 31002, probe-confirmed by its drift)
#              prior 1.8 (per-PV-string block read 31025 + decode_pv_strings();
#              absent-latch so an install without the block stops probing it)
#              prior 1.7 (internal RLock, connect health probe + escalating
#              back-off, outage early-abort, pvPowerWatts critical,
#              verify-mismatch=False)
#
# Register map reviewed against Sigenergy Modbus Protocol V2.9 (2026-05-13).
# V2.9 is STILL the current protocol as of 21-07-2026 — re-checked that day after
# the mySigen app 4.0 launch (Intersolar, 17-19 Jun 2026). App 4.0 is a cloud-side
# release (SigenAgent trading, AI search) and did NOT bump the Modbus spec.
# (Was verified against V2.8 (2025-11-28); V2.9 deltas applied: 40031 mode 0x07
#  is "Reserved" (not "AI Mode"), 0x08="V2G" added; 40032/40034 are GLOBAL caps
#  "regardless of EMS mode". Not yet used: 40001 PCS active-power dispatch
#  (S32 kW; needs 40029=1 + 40031=0; no command watchdog; verify sign on hardware).)
# DELIBERATELY NOT IMPLEMENTED from V2.9: the ESS pre-heating block (50000-50183)
# and the PID/PSS device ranges. See the pre-heating note below the holding
# registers for why — the block is absent on our firmware.
# Adapted from SigenergySolar v3.1 sigenergy_modbus.py
# Changes from SigenergySolar version:
#   - Added set_export_limit(watts) wrapper for register 40038-39
#   - Fixed read_discharge_cutoff() bugs (throttle, address reference, register offset)
#   - Updated logger name to SigenEnergyManager
# v1.5 (30-04-2026):
#   - sleep_func injection so plugin thread can interrupt long throttle sleeps
#     during shutdown (read_all does ~16 reads × 1s, was blocking StopThread).
#   - Writes now mark connection invalid on result.isError() so a failed write
#     triggers a reconnect on the next operation instead of zombie state.

import logging
import threading
import time
from datetime import datetime

try:
    from pymodbus.client import ModbusTcpClient
    from pymodbus.exceptions import ModbusException, ConnectionException
    PYMODBUS_AVAILABLE = True
except ImportError:
    PYMODBUS_AVAILABLE = False


# ============================================================
# Constants - Sigenergy Modbus Register Map
# Reference: Sigenergy Modbus Protocol V2.9 (2026-05-13)
# ============================================================

# --- Plant registers (slave address 247, read-only, function 0x03) ---

PLANT_EMS_WORK_MODE        = 30003    # U16: 0=Max self consumption, 1=AI, 2=TOU, 7=Remote EMS
PLANT_GRID_SENSOR_STATUS   = 30004    # U16: 0=not connected, 1=connected
PLANT_GRID_ACTIVE_POWER    = 30005    # S32 (2 regs), gain 1000, kW. >0=import, <0=export
PLANT_ON_OFF_GRID_STATUS   = 30009    # U16: 0=on-grid, 1=off-grid(auto), 2=off-grid(manual)
PLANT_BATTERY_SOC          = 30014    # U16, gain 10, %
PLANT_PV_POWER             = 30035    # S32 (2 regs), gain 1000, kW
PLANT_ESS_POWER            = 30037    # S32 (2 regs), gain 1000, kW. >0=charge, <0=discharge
PLANT_RUNNING_STATE        = 30051    # U16: 0=Standby, 1=Running, 2=Fault, 3=Shutdown
PLANT_ESS_DISCHARGE_CUTOFF = 30086    # U16, gain 10, %
PLANT_ESS_SOH              = 30087    # U16, gain 10, % (weighted average)
PLANT_PV_TOTAL_KWH         = 30088    # U64 (4 regs), gain 100, kWh — LIFETIME PV generation
PLANT_LOAD_DAILY_KWH       = 30092    # U32 (2 regs), gain 100, kWh — daily reset at midnight
PLANT_TOTAL_IMPORT_KWH     = 30216    # U64 (4 regs), gain 100, kWh — LIFETIME grid import
PLANT_TOTAL_EXPORT_KWH     = 30220    # U64 (4 regs), gain 100, kWh — LIFETIME grid export

# --- Inverter registers (slave address 1-246, read-only, function 0x03) ---

INV_DAILY_CHARGE_ENERGY    = 30566    # U32 (2 regs), gain 100, kWh
INV_DAILY_DISCHARGE_ENERGY = 30572    # U32 (2 regs), gain 100, kWh
INV_BATTERY_AVG_TEMP       = 30603    # S16, gain 10, degC
INV_BATTERY_AVG_VOLTAGE    = 30604    # U16, gain 1000, V
INV_BATTERY_MAX_TEMP       = 30620    # S16, gain 10, degC
INV_BATTERY_MIN_TEMP       = 30621    # S16, gain 10, degC
# Per-PV-string block, probed live on this SigenStor 10 kW 1ph 13-08-2026:
# [string_count, mppt_count, V1, I1, V2, I2, V3, I3, V4, I4]. V gain 10
# (200-320 V here — string voltages match the panel counts, 9x~34 V ≈ 310 V,
# 6x ≈ 205 V), I gain 100. The four V*I powers summed to the plant PV total
# +6.8% (DC side vs AC total), which is the confirmation the pairs are real.
# Read EXACTLY this many registers: 31035+ is undefined on this firmware and
# ONE undefined address inside a block read fails the WHOLE transaction.
INV_PV_STRING_BLOCK        = 31025    # U16 x 10 (count + mppts + 4 V/I pairs)
INV_PV_STRING_BLOCK_COUNT  = 10
# Grid frequency, probed and CONFIRMED live 13-08-2026 by sampling: it drifted
# 49.98 -> 49.95 -> 49.96 Hz over 80 s, which nothing but mains frequency does.
# Its neighbour 31001 sat at exactly 5000 throughout, so that one is the
# NOMINAL 50.00 Hz rating, not a measurement — as is 31000 at exactly 230.0 V.
# A register that never moves is a nameplate, not a reading.
INV_GRID_FREQUENCY_HZ      = 31002    # U16, gain 100, Hz
# All of the following are named from the OFFICIAL Sigenergy Modbus Protocol
# V2.7 (the public PDF; sigenergy.com serves it only to a browser User-Agent,
# a bare curl gets a "Blocked" HTML page). Reading the spec named five
# registers this plugin had probed but could not identify — and confirmed
# every empirical call, including that 31000/31001 are RATED values and 31002
# is the live measurement.
INV_PCS_INTERNAL_TEMP_C    = 31003    # S16, gain 10, degC — the inverter's OWN
                                      # temperature. An inverter running hot
                                      # is a real fault signal and nothing in
                                      # the estate was watching it.
INV_PACK_COUNT             = 31024    # U16 — how many battery PACKS the stack
                                      # holds, straight from the hardware.
                                      # Better than dividing capacity by a
                                      # module size we assume.
INV_INSULATION_RESISTANCE  = 31037    # U16, gain 1000, MOhm — PV array
                                      # insulation. A falling value means
                                      # moisture ingress or damaged cable,
                                      # which is a safety matter, not a
                                      # performance one.
INV_ALARM1                 = 30605    # U16 bitfield (Appendix 2). 0 = clear.
# THE REAL grid voltage — not 31000, which is the 230.0 V nameplate. This one
# read 252.21 V on 13-08-2026 against a UK statutory ceiling of 253.0 V
# (230 V +10%), and an inverter must curtail or disconnect above it. So a
# high reading here is money: export gets cut, and the cause is the DNO's
# network rather than anything in this house.
INV_PHASE_A_VOLTAGE        = 31011    # U32 (2 regs), gain 100, V
INV_PHASE_A_CURRENT        = 31017    # U32 (2 regs), gain 100, A
INV_RATED_CAPACITY_KWH     = 30548    # U32 (2 regs), gain 100, kWh — the pack's
                                      # NAMEPLATE. Reads 36.16 here, agreeing
                                      # exactly with the plant's own 30083 and
                                      # with the cloud, while the plugin was
                                      # configured for 35.04. Published for
                                      # comparison, NOT used for control: the
                                      # measured SOC-to-kWh relationship (~35.6)
                                      # is what a percentage actually converts
                                      # at, and rated is not the same thing.

# --- Plant holding registers (slave address 247, read/write) ---
# Read with function 0x03, write single with 0x06, write multiple with 0x10

HOLD_REMOTE_EMS_ENABLE     = 40029    # U16 RW: 0=disabled, 1=enabled
HOLD_REMOTE_EMS_MODE       = 40031    # U16 RW: Remote EMS control mode (Appendix 6)
HOLD_ESS_MAX_CHARGE        = 40032    # U32 RW (2 regs), gain 1000, kW. GLOBAL cap, all EMS modes (V2.9)
HOLD_ESS_MAX_DISCHARGE     = 40034    # U32 RW (2 regs), gain 1000, kW. GLOBAL cap, all EMS modes (V2.9)
HOLD_GRID_MAX_EXPORT_LIMIT = 40038    # U32 RW (2 regs), gain 1000, kW. Requires grid sensor.
HOLD_GRID_MAX_IMPORT_LIMIT = 40040    # U32 RW (2 regs), gain 1000, kW.
HOLD_ESS_BACKUP_SOC        = 40046    # U16 RW, gain 10, % - backup reserve SOC
HOLD_ESS_CHARGE_CUTOFF     = 40047    # U16 RW, gain 10, % - max charge SOC
HOLD_ESS_DISCHARGE_CUTOFF  = 40048    # U16 RW, gain 10, % - min discharge SOC (reserve protection)

# --- ESS pre-heating (V2.9, plant holding registers) - NOT IMPLEMENTED ---
#
# V2.9 added a battery pre-heating block. Warming a cold pack lifts its charge
# acceptance, so on paper it is worth having: a derated winter morning costs us
# solar we cannot get back. The map (two independent community transcriptions
# agree exactly, and they self-check — the 30 TOU slots run 50003..50182, ending
# immediately before the reserved-SoC register):
#
#   50000            U16 RW  Pre-heating enable        0=disable, 1=enable
#   50001            U16 RW  Pre-heating mode          0=automatic, 1=manual
#   50002            U16 RW  Pre-heating advance       0/1, only when mode=manual
#   50003+(n-1)*6    U32 RW  TOU slot n start, epoch seconds  (n = 1..30)
#   50005+(n-1)*6    U32 RW  TOU slot n end,   epoch seconds
#   50007+(n-1)*6    S32 RW  TOU slot n target power, gain 1000, kW (<0 discharge)
#   50183            U16 RW  Pre-heating reserved SoC, gain 100, %
#
# We do NOT read any of these, because OUR INVERTER DOES NOT HAVE THEM. Probed
# live on 21-07-2026 against a SigenStor 10 kW 1ph (plant address 247, firmware
# V100R001C22SPC113): every one of the addresses above returns Modbus exception
# 2, ILLEGAL DATA ADDRESS. So does every other probe across the whole 50k range
# (49999 / 50100 / 50200 / 50500) — the range is simply not implemented in this
# firmware, rather than pre-heating alone being switched off. Control reads in
# the same session (30003, 30014) answered normally, so this is not a comms
# fault. sigenergy2mqtt reaches the same conclusion at runtime: it probes 50000
# and skips the whole pre-heating device when the read fails.
#
# Implementing them today would add reads that fail on every single poll cycle,
# each one burning a second of the throttle and logging an error, in exchange for
# no data. Revisit after an inverter firmware update: re-probe 50000, and if it
# answers, wire up 50000/50001/50002/50183 (skip the 30 TOU slots — 90 registers
# is far too many for a 1s-throttled cycle, and they only pay off for scheduled
# arbitrage, which we do not do on the Tracker tariff).

# Sanity ceiling for power-limit writes (watts). 3x the largest residential
# Sigenergy inverter (10kW), so a value above this is certainly a bug/typo —
# the setters clamp to it with a warning rather than writing garbage to the
# inverter (which applies its own internal clamp anyway).
MAX_POWER_LIMIT_W = 30_000

# --- EMS work modes (register 30003) ---

EMS_MODES = {
    0: "Max Self Consumption",
    1: "AI Mode",
    2: "TOU",
    5: "Full Feed-in to Grid",
    7: "Remote EMS",
    9: "Custom",
}

# --- Remote EMS control modes (register 40031, Appendix 6) ---

REMOTE_EMS_MODES = {
    0x00: "PCS Remote Control",   # active-power dispatch via 40001 (S32 kW); we don't use this yet
    0x01: "Standby",
    0x02: "Max Self Consumption",
    0x03: "Charge Grid First",
    0x04: "Charge PV First",
    0x05: "Discharge PV First",
    0x06: "Discharge ESS First",
    0x07: "Reserved",             # was mislabelled "AI Mode" pre-V2.9; 0x07 is Reserved
    0x08: "V2G",                  # added in Protocol V2.9 (2026-05-13)
}

PLANT_RUNNING_STATES = {
    0x00: "Standby",
    0x01: "Running",
    0x02: "Fault",
    0x03: "Shutdown",
}

GRID_STATUSES = {
    0: "On-grid",
    1: "Off-grid (auto)",
    2: "Off-grid (manual)",
}

# Protocol timing - 1000ms minimum between requests per Sigenergy V2.8 spec
MIN_REQUEST_INTERVAL = 1.0


# Physical bounds for one PV string on a residential inverter. Anything
# outside these means the block did not decode to what we think it is, so
# nothing in it can be trusted — see decode_pv_strings.
PV_STRING_MAX_VOLTS = 1500.0    # Sigenergy max PV input is 1000 V DC
PV_STRING_MAX_AMPS  = 100.0     # per-MPPT max input current is ~16-32 A


def _s16(word):
    """Reinterpret one raw Modbus word as a SIGNED 16-bit integer."""
    value = int(word)
    if value >= 32768:
        value -= 65536
    return value


def decode_pv_strings(regs):
    """Decode the 31025 per-string block into [{"v", "a", "w"}, ...].

    regs is the raw U16 list [count, mppts, V1, I1, ...]. Pure and
    side-effect-free — the test seam. The count register is trusted only up
    to the number of V/I pairs the block actually carries (4 in a 10-register
    read), so a bigger inverter reporting 6 strings yields the first 4 rather
    than an index error. Anything short or malformed returns [] — an absent
    reading must never fabricate a string at 0 W.

    THE V/I WORDS ARE SIGNED (S16), live-confirmed 03-09-2026. Read as
    unsigned, a string sitting at its dawn/dusk zero-crossing reported
    65532-65535 raw — which is -4..-1 as S16, i.e. -0.04..-0.01 A of sensor
    offset — as 655.32-655.35 A, and V*I then put 215 kW on a 4.275 kWp
    string. It had done so at dawn and dusk on 21 days since the per-string
    block shipped, because both the probe that established this block and the
    test fixture built from it were taken in full sun, where the sign never
    shows. The official protocol PDF is not to hand; the evidence is the
    wrap signature itself, which no unsigned reading explains.

    A decoded pair outside PV_STRING_MAX_* means the block is not the shape
    we think it is, so the WHOLE read is discarded rather than one string
    patched — a misaligned block has no trustworthy members.

    Watts are floored at 0. A string cannot generate negative power, the
    small negative current at the zero-crossing is instrument offset, and a
    negative would subtract from the day's integrated kWh downstream.
    """
    if not regs or len(regs) < 4:
        return []
    try:
        count = int(regs[0])
    except (TypeError, ValueError):
        return []
    pairs_available = (len(regs) - 2) // 2
    count = max(0, min(count, pairs_available))
    out = []
    for i in range(count):
        try:
            volts = _s16(regs[2 + i * 2]) / 10.0
            amps = _s16(regs[3 + i * 2]) / 100.0
        except (TypeError, ValueError):
            return []
        if abs(volts) > PV_STRING_MAX_VOLTS or abs(amps) > PV_STRING_MAX_AMPS:
            return []
        out.append({"v": round(volts, 1), "a": round(amps, 2),
                    "w": max(0, int(round(volts * amps)))})
    return out


def _locked(fn):
    """Serialise a primitive on the instance's RLock.

    The sync pymodbus client, _connected and _throttle's clock are shared
    state; plugin.py's _state_lock serialises the main callers but unlocked
    paths (diagnostic menus, future callers) could interleave a connect()
    with an in-flight transaction. RLock, so a write's verify read-back and
    read_all's per-register reads nest without deadlock.
    """
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__  = fn.__doc__
    return wrapper


class SigenergyModbus:
    """Modbus TCP client for Sigenergy inverter.

    Reads from two slave addresses:
      - Plant address (247): aggregated system data
      - Inverter address (1-246): individual inverter + battery data

    Writes to plant address (247) via Remote EMS control registers.

    Sign conventions:
      gridPowerWatts:    >0 = importing from grid, <0 = exporting to grid
      batteryPowerWatts: >0 = charging, <0 = discharging
      pvPowerWatts:      always >= 0
      homePowerWatts:    always >= 0 (calculated: PV + Grid - Battery)
    """

    def __init__(self, ip, port=502, plant_address=247, inverter_address=1,
                 logger=None, sleep_func=None, inverter_max_w=10000):
        self.ip               = ip
        self.port             = port
        self.plant_address    = plant_address
        self.inverter_address = inverter_address
        # The inverter's rated power, in watts. ONE source of truth on the object
        # rather than a default repeated at each call site: set_self_consumption()
        # used to reset both limits to a hardcoded 10000, which is exactly right on
        # a 10 kW inverter and silently wrong on any other — it capped battery
        # discharge for a whole verify interval after every return to
        # self-consumption. Keeping it here means a new mode method cannot
        # reintroduce the hardcode by omission. The plugin keeps it in step with
        # the inverterMaxKw pref (set at construction and on every prefs save).
        self.inverter_max_w   = int(inverter_max_w or 10000)
        self.logger           = logger or logging.getLogger("SigenEnergyManager.Modbus")
        self.client           = None
        self._connected       = False
        self._lock            = threading.RLock()   # serialises primitives (see _locked)
        self._last_connect_attempt = 0
        # Reconnect delay escalates 30s -> 60s -> 120s (capped) while the
        # inverter stays unreachable, resetting to 30s on a healthy connect —
        # stops a hard outage re-running a burst every 30s indefinitely.
        self._reconnect_delay_base = 30
        self._reconnect_delay_max  = 120
        self._reconnect_delay      = self._reconnect_delay_base
        self._last_request_time    = 0.0
        # Per-string block absent-latch. The 31025 block exists on this
        # firmware but may not on others (the 50000 pre-heat lesson: a spec
        # register is not a hardware register). Three consecutive failures of
        # ONLY this block latch it off for the life of the process, so an
        # install without it does not burn a throttled failing read — and a
        # debug line — every cycle for ever. A restart re-probes.
        self._pv_strings_absent    = False
        self._pv_strings_misses    = 0
        # sleep_func: callable taking seconds. When called from a plugin thread,
        # pass plugin.sleep so StopThread can interrupt the 1s throttle delay
        # between Modbus requests (read_all does ~16 reads = up to 16s blocking).
        # Defaults to time.sleep for standalone/test use.
        self._sleep            = sleep_func or time.sleep

    @property
    def connected(self):
        return self._connected

    # ================================================================
    # Connection Management
    # ================================================================

    @_locked
    def connect(self):
        """Connect to the Sigenergy inverter via Modbus TCP.

        A successful TCP handshake alone is not proof of health — during an
        inverter reboot/firmware update the device accepts TCP while the Modbus
        application layer is down, and treating that as connected re-runs a
        full failed read cycle every reconnect. So after connecting, one cheap
        probe read (EMS work mode) must succeed before we report healthy.
        The reconnect delay escalates while attempts keep failing.
        """
        if not PYMODBUS_AVAILABLE:
            self.logger.error("pymodbus not installed - cannot connect to inverter")
            return False

        now = time.monotonic()   # monotonic — immune to NTP wall-clock steps
        if now - self._last_connect_attempt < self._reconnect_delay:
            return False

        self._last_connect_attempt = now

        try:
            if self.client:
                self.client.close()

            self.client = ModbusTcpClient(
                host=self.ip,
                port=self.port,
                timeout=10,
                retries=3,
            )

            result = self.client.connect()
            if result:
                # Tentatively connected — verify the application layer with a
                # probe read before declaring healthy.
                self._connected = True
                if self._read_uint16(PLANT_EMS_WORK_MODE) is None:
                    self.logger.warning(
                        f"TCP connected to {self.ip}:{self.port} but probe read "
                        f"failed — Modbus layer not ready (retry in "
                        f"{self._next_reconnect_delay()}s)"
                    )
                    self._connected = False
                    return False
                self._reconnect_delay = self._reconnect_delay_base
                self.logger.info(
                    f"Connected to Sigenergy at {self.ip}:{self.port} "
                    f"(plant={self.plant_address}, inverter={self.inverter_address})"
                )
                return True
            else:
                self._connected = False
                self.logger.warning(
                    f"Failed to connect to inverter at {self.ip}:{self.port} "
                    f"(retry in {self._next_reconnect_delay()}s)")
                return False

        except Exception as e:
            self._connected = False
            self._next_reconnect_delay()
            self.logger.error(f"Modbus connection error: {e}")
            return False

    def _next_reconnect_delay(self):
        """Escalate the reconnect delay (base -> 2x -> max, capped) and return it."""
        self._reconnect_delay = min(self._reconnect_delay * 2,
                                    self._reconnect_delay_max)
        return self._reconnect_delay

    @_locked
    def disconnect(self):
        """Disconnect from the inverter."""
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self._connected = False
        self.logger.info("Disconnected from Sigenergy inverter")

    # ================================================================
    # Request Throttling
    # ================================================================

    def _throttle(self):
        """Enforce 1000ms minimum between Modbus requests per protocol spec.

        Uses the sleep_func injected at construction time so when called from
        a plugin thread the sleep can be interrupted by StopThread during
        shutdown (Indigo hard-kills plugins that don't respond within ~10s).
        """
        # time.monotonic() — a backwards NTP wall-clock step would make the
        # elapsed value negative and stall for the full step size. The min()
        # bound is belt-and-braces: never sleep longer than one interval.
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            self._sleep(min(MIN_REQUEST_INTERVAL - elapsed, MIN_REQUEST_INTERVAL))
        self._last_request_time = time.monotonic()

    # ================================================================
    # Low-Level Read Primitives (function 0x03 - holding registers)
    # ================================================================

    @_locked
    def _read_int16(self, register, slave=None):
        """Read a signed 16-bit register."""
        if slave is None:
            slave = self.plant_address
        if not self._connected:
            # A transport failure earlier in this cycle already marked the
            # connection dead — abort instantly instead of burning a throttled
            # read (and an ERROR line) per remaining register. read_all()'s
            # error counting still sees the None and disconnects cleanly.
            return None
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=1, device_id=slave
            )
            if result.isError():
                self.logger.debug(f"Error reading reg {register} (slave {slave}): {result}")
                return None
            value = result.registers[0]
            if value >= 32768:
                value -= 65536
            return value
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus read error reg {register} (slave {slave}): {e}")
            self._connected = False
            return None
        except Exception as e:
            # Raw socket errors (BrokenPipeError, OSError) are NOT always wrapped
            # in ModbusException by pymodbus — treat any transport-level surprise
            # as a dead connection so the cycle aborts instead of retrying 20x.
            self.logger.error(f"Unexpected read error reg {register} (slave {slave}): {e}")
            self._connected = False
            return None

    @_locked
    def _read_uint16(self, register, slave=None):
        """Read an unsigned 16-bit register."""
        if slave is None:
            slave = self.plant_address
        if not self._connected:
            # A transport failure earlier in this cycle already marked the
            # connection dead — abort instantly instead of burning a throttled
            # read (and an ERROR line) per remaining register. read_all()'s
            # error counting still sees the None and disconnects cleanly.
            return None
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=1, device_id=slave
            )
            if result.isError():
                self.logger.debug(f"Error reading reg {register} (slave {slave}): {result}")
                return None
            return result.registers[0]
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus read error reg {register} (slave {slave}): {e}")
            self._connected = False
            return None
        except Exception as e:
            # Raw socket errors (BrokenPipeError, OSError) are NOT always wrapped
            # in ModbusException by pymodbus — treat any transport-level surprise
            # as a dead connection so the cycle aborts instead of retrying 20x.
            self.logger.error(f"Unexpected read error reg {register} (slave {slave}): {e}")
            self._connected = False
            return None

    @_locked
    def _read_block_u16(self, register, count, slave=None):
        """Read `count` consecutive U16 registers in ONE transaction.

        Returns the raw register list, or None on any failure. One throttled
        request however many registers, which is why the per-string block is
        a single call rather than ten _read_uint16s. NB a block containing
        even one undefined address fails whole — size reads exactly.
        """
        if slave is None:
            slave = self.plant_address
        if not self._connected:
            return None
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=count, device_id=slave
            )
            if result.isError():
                self.logger.debug(
                    f"Error reading regs {register}-{register + count - 1} "
                    f"(slave {slave}): {result}")
                return None
            return list(result.registers)
        except (ModbusException, ConnectionException) as e:
            self.logger.error(
                f"Modbus read error regs {register}-{register + count - 1} "
                f"(slave {slave}): {e}")
            self._connected = False
            return None
        except Exception as e:
            self.logger.error(
                f"Unexpected read error regs {register}-{register + count - 1} "
                f"(slave {slave}): {e}")
            self._connected = False
            return None

    @_locked
    def _read_int32(self, register, slave=None):
        """Read a signed 32-bit value from two consecutive registers."""
        if slave is None:
            slave = self.plant_address
        if not self._connected:
            # A transport failure earlier in this cycle already marked the
            # connection dead — abort instantly instead of burning a throttled
            # read (and an ERROR line) per remaining register. read_all()'s
            # error counting still sees the None and disconnects cleanly.
            return None
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=2, device_id=slave
            )
            if result.isError():
                self.logger.debug(
                    f"Error reading regs {register}-{register+1} (slave {slave}): {result}"
                )
                return None
            value = (result.registers[0] << 16) | result.registers[1]
            if value >= 2147483648:
                value -= 4294967296
            return value
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus read error regs {register}-{register+1} (slave {slave}): {e}")
            self._connected = False
            return None
        except Exception as e:
            self.logger.error(f"Unexpected read error regs {register}-{register+1} (slave {slave}): {e}")
            self._connected = False
            return None

    @_locked
    def _read_uint64(self, register, slave=None):
        """Read an unsigned 64-bit value from four consecutive registers (big-endian)."""
        if slave is None:
            slave = self.plant_address
        if not self._connected:
            # A transport failure earlier in this cycle already marked the
            # connection dead — abort instantly instead of burning a throttled
            # read (and an ERROR line) per remaining register. read_all()'s
            # error counting still sees the None and disconnects cleanly.
            return None
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=4, device_id=slave
            )
            if result.isError():
                self.logger.debug(
                    f"Error reading regs {register}-{register+3} (slave {slave}): {result}"
                )
                return None
            r = result.registers
            return (r[0] << 48) | (r[1] << 32) | (r[2] << 16) | r[3]
        except (ModbusException, ConnectionException) as e:
            self.logger.error(
                f"Modbus read error regs {register}-{register+3} (slave {slave}): {e}"
            )
            self._connected = False
            return None
        except Exception as e:
            self.logger.error(
                f"Unexpected read error regs {register}-{register+3} (slave {slave}): {e}"
            )
            self._connected = False
            return None

    @_locked
    def _read_uint32(self, register, slave=None):
        """Read an unsigned 32-bit value from two consecutive registers."""
        if slave is None:
            slave = self.plant_address
        if not self._connected:
            # A transport failure earlier in this cycle already marked the
            # connection dead — abort instantly instead of burning a throttled
            # read (and an ERROR line) per remaining register. read_all()'s
            # error counting still sees the None and disconnects cleanly.
            return None
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=2, device_id=slave
            )
            if result.isError():
                self.logger.debug(
                    f"Error reading regs {register}-{register+1} (slave {slave}): {result}"
                )
                return None
            return (result.registers[0] << 16) | result.registers[1]
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus read error regs {register}-{register+1} (slave {slave}): {e}")
            self._connected = False
            return None
        except Exception as e:
            self.logger.error(f"Unexpected read error regs {register}-{register+1} (slave {slave}): {e}")
            self._connected = False
            return None

    # ================================================================
    # Main Read Function
    # ================================================================

    def read_all(self):
        """Read all key registers and return a data dict.

        Returns None if connection fails (too many errors).

        Data keys returned:
          emsWorkMode, gridSensorConnected, gridPowerWatts, gridStatus,
          batterySoc, pvPowerWatts, batteryPowerWatts, plantRunningState,
          dischargeCutoffSoc, batterySoh, batteryDailyChargeKwh,
          batteryDailyDischargeKwh, batteryTempC, batteryCellVoltage,
          batteryMaxTempC, batteryMinTempC, homePowerWatts,
          modbusConnected, lastUpdate,
          pvStrings (v1.8 — [{v, a, w}, ...] per PV string; key absent when
          the block fails or the firmware lacks it, never an empty guess)
        """
        if not self._connected:
            if not self.connect():
                return None

        data         = {}
        plant_errors = 0
        inv_errors   = 0

        # --- Phase A: Plant reads (slave 247) ---

        ems_mode = self._read_uint16(PLANT_EMS_WORK_MODE)
        if ems_mode is not None:
            data["emsWorkMode"] = EMS_MODES.get(ems_mode, f"Unknown ({ems_mode})")
        else:
            plant_errors += 1

        grid_sensor = self._read_uint16(PLANT_GRID_SENSOR_STATUS)
        if grid_sensor is not None:
            data["gridSensorConnected"] = (grid_sensor == 1)
        else:
            plant_errors += 1

        grid_power = self._read_int32(PLANT_GRID_ACTIVE_POWER)
        if grid_power is not None:
            data["gridPowerWatts"] = grid_power
        else:
            plant_errors += 1

        grid_status = self._read_uint16(PLANT_ON_OFF_GRID_STATUS)
        if grid_status is not None:
            data["gridStatus"] = GRID_STATUSES.get(grid_status, f"Unknown ({grid_status})")
        else:
            plant_errors += 1

        batt_soc = self._read_uint16(PLANT_BATTERY_SOC)
        if batt_soc is not None:
            data["batterySoc"] = round(batt_soc / 10.0, 1)
        else:
            plant_errors += 1

        pv_power = self._read_int32(PLANT_PV_POWER)
        if pv_power is not None:
            data["pvPowerWatts"] = max(0, pv_power)
        else:
            plant_errors += 1

        batt_power = self._read_int32(PLANT_ESS_POWER)
        if batt_power is not None:
            data["batteryPowerWatts"] = batt_power
        else:
            plant_errors += 1

        running_state = self._read_uint16(PLANT_RUNNING_STATE)
        if running_state is not None:
            data["plantRunningState"] = PLANT_RUNNING_STATES.get(
                running_state, f"Unknown ({running_state})"
            )
        else:
            plant_errors += 1

        cutoff_soc = self._read_uint16(PLANT_ESS_DISCHARGE_CUTOFF)
        if cutoff_soc is not None:
            data["dischargeCutoffSoc"] = round(cutoff_soc / 10.0, 1)
        else:
            plant_errors += 1

        batt_soh = self._read_uint16(PLANT_ESS_SOH)
        if batt_soh is not None:
            data["batterySoh"] = round(batt_soh / 10.0, 1)
        else:
            plant_errors += 1

        # --- Phase B: Inverter reads (configurable slave address) ---

        inv_addr = self.inverter_address

        daily_charge = self._read_uint32(INV_DAILY_CHARGE_ENERGY, slave=inv_addr)
        if daily_charge is not None:
            data["batteryDailyChargeKwh"] = round(daily_charge / 100.0, 2)
        else:
            inv_errors += 1

        daily_discharge = self._read_uint32(INV_DAILY_DISCHARGE_ENERGY, slave=inv_addr)
        if daily_discharge is not None:
            data["batteryDailyDischargeKwh"] = round(daily_discharge / 100.0, 2)
        else:
            inv_errors += 1

        batt_temp = self._read_int16(INV_BATTERY_AVG_TEMP, slave=inv_addr)
        if batt_temp is not None:
            data["batteryTempC"] = round(batt_temp / 10.0, 1)
        else:
            inv_errors += 1

        batt_voltage = self._read_uint16(INV_BATTERY_AVG_VOLTAGE, slave=inv_addr)
        if batt_voltage is not None:
            data["batteryCellVoltage"] = round(batt_voltage / 1000.0, 3)
        else:
            inv_errors += 1

        batt_max_temp = self._read_int16(INV_BATTERY_MAX_TEMP, slave=inv_addr)
        if batt_max_temp is not None:
            data["batteryMaxTempC"] = round(batt_max_temp / 10.0, 1)
        else:
            inv_errors += 1

        batt_min_temp = self._read_int16(INV_BATTERY_MIN_TEMP, slave=inv_addr)
        if batt_min_temp is not None:
            data["batteryMinTempC"] = round(batt_min_temp / 10.0, 1)
        else:
            inv_errors += 1

        # Grid frequency (v1.9). NON-critical: a failure costs this key, never
        # the snapshot. Worth having beside the VPP work — a grid event is
        # ultimately a frequency problem, so this is the quantity the whole
        # scheme exists to defend.
        grid_hz = self._read_uint16(INV_GRID_FREQUENCY_HZ, slave=inv_addr)
        if grid_hz is not None:
            data["gridFrequencyHz"] = round(grid_hz / 100.0, 2)
        else:
            inv_errors += 1

        # Inverter self-diagnostics (v1.10), all named from the official V2.7
        # protocol. Every one is NON-critical: a failure costs its own key and
        # never the snapshot.
        pcs_temp = self._read_int16(INV_PCS_INTERNAL_TEMP_C, slave=inv_addr)
        if pcs_temp is not None:
            data["pcsInternalTempC"] = round(pcs_temp / 10.0, 1)
        else:
            inv_errors += 1

        insul = self._read_uint16(INV_INSULATION_RESISTANCE, slave=inv_addr)
        if insul is not None:
            data["insulationResistanceMohm"] = round(insul / 1000.0, 3)
        else:
            inv_errors += 1

        packs = self._read_uint16(INV_PACK_COUNT, slave=inv_addr)
        if packs is not None:
            data["packCount"] = packs
        else:
            inv_errors += 1

        alarm1 = self._read_uint16(INV_ALARM1, slave=inv_addr)
        if alarm1 is not None:
            data["alarm1Raw"] = alarm1
        else:
            inv_errors += 1

        rated = self._read_uint32(INV_RATED_CAPACITY_KWH, slave=inv_addr)
        if rated is not None:
            data["ratedCapacityKwh"] = round(rated / 100.0, 2)
        else:
            inv_errors += 1

        volts = self._read_uint32(INV_PHASE_A_VOLTAGE, slave=inv_addr)
        # 0xFFFFFFFF is this firmware's "not applicable" for an unused phase,
        # and it decodes to a nonsense 42949672.95 V if taken at face value.
        if volts is not None and volts != 0xFFFFFFFF:
            data["gridVoltageV"] = round(volts / 100.0, 2)
        else:
            inv_errors += 1

        amps = self._read_uint32(INV_PHASE_A_CURRENT, slave=inv_addr)
        if amps is not None and amps != 0xFFFFFFFF:
            data["gridCurrentA"] = round(amps / 100.0, 2)
        else:
            inv_errors += 1

        # Per-PV-string block (v1.8) — one transaction for all four V/I pairs.
        # NON-critical by design: a failure costs the pvStrings key this cycle,
        # never the snapshot. The absent-latch stops an install whose firmware
        # lacks the block from paying a failing throttled read every cycle.
        if not self._pv_strings_absent:
            string_regs = self._read_block_u16(
                INV_PV_STRING_BLOCK, INV_PV_STRING_BLOCK_COUNT, slave=inv_addr)
            if string_regs is not None:
                data["pvStrings"] = decode_pv_strings(string_regs)
                self._pv_strings_misses = 0
            else:
                inv_errors += 1
                if self._connected:
                    # The link is up and only this block failed — count it as
                    # a real "register absent" strike, not an outage symptom.
                    self._pv_strings_misses += 1
                    if self._pv_strings_misses >= 3:
                        self._pv_strings_absent = True
                        self.logger.info(
                            "Per-PV-string registers (31025+) not answering on "
                            "this inverter — per-string readings disabled until "
                            "the next plugin restart.")

        # --- Phase D: Plant daily/lifetime energy registers ---
        # pvLifetimeKwh / gridImportLifetimeKwh / gridExportLifetimeKwh are LIFETIME
        # totals; plugin.py computes daily values as (current - start-of-day snapshot).
        # homeDailyDirectKwh (30092) resets at midnight on the inverter — read directly.

        pv_total = self._read_uint64(PLANT_PV_TOTAL_KWH)
        if pv_total is not None:
            data["pvLifetimeKwh"] = round(pv_total / 100.0, 2)
        else:
            plant_errors += 1

        load_daily = self._read_uint32(PLANT_LOAD_DAILY_KWH)
        if load_daily is not None:
            data["homeDailyDirectKwh"] = round(load_daily / 100.0, 2)
        else:
            plant_errors += 1

        import_total = self._read_uint64(PLANT_TOTAL_IMPORT_KWH)
        if import_total is not None:
            data["gridImportLifetimeKwh"] = round(import_total / 100.0, 2)
        else:
            plant_errors += 1

        export_total = self._read_uint64(PLANT_TOTAL_EXPORT_KWH)
        if export_total is not None:
            data["gridExportLifetimeKwh"] = round(export_total / 100.0, 2)
        else:
            plant_errors += 1

        # --- Connection quality check ---

        # read_all issues this many register reads per cycle (Phase A=10, B=15
        # incl. the per-string block, D=4). Keep in step if reads are
        # added/removed so the "more than half failed" disconnect threshold and
        # the error-ratio log lines stay self-consistent. Once the per-string
        # absent-latch engages the real count drops back to 20 — acceptable
        # slack in a >half threshold, not worth a moving constant.
        TOTAL_READS  = 29
        total_errors = plant_errors + inv_errors
        if total_errors > TOTAL_READS // 2:  # more than half of the reads failed
            self.logger.error(
                f"Too many Modbus errors ({total_errors}/{TOTAL_READS}) - marking disconnected")
            self._connected = False
            # Stamp the attempt clock so the next poll honours the reconnect
            # delay instead of instantly re-running a failed cycle.
            self._last_connect_attempt = time.monotonic()
            return None

        # Critical-register guard. A partial read drops the failed key entirely;
        # every consumer then does .get("batterySoc", 0.0), so a single transient
        # SOC-register failure (slave busy / CRC) would feed the manager a phantom
        # 0% SOC — a force-charge that never completes (keeps importing) or a
        # force-charge of an already-full battery. If any critical register failed
        # this cycle, return None so _poll_modbus keeps the last-known-good snapshot
        # instead of acting on fabricated zeros. The connection is healthy, so we
        # do NOT flip self._connected — the next poll retries normally.
        CRITICAL_KEYS = (
            "batterySoc", "gridPowerWatts", "batteryPowerWatts",
            "pvPowerWatts", "plantRunningState", "gridStatus",
        )
        missing_critical = [k for k in CRITICAL_KEYS if k not in data]
        if missing_critical:
            self.logger.warning(
                f"Partial Modbus read — critical register(s) missing "
                f"{missing_critical} ({total_errors}/{TOTAL_READS} errors); keeping "
                f"last-known-good snapshot this cycle (not acting on partial data)."
            )
            return None

        # --- Calculated values (after the critical guard, so never from
        #     fabricated .get(..., 0) defaults) ---
        data["homePowerWatts"] = max(
            0, data["pvPowerWatts"] + data["gridPowerWatts"] - data["batteryPowerWatts"]
        )

        data["modbusConnected"] = True
        data["lastUpdate"]      = datetime.now().strftime("%H:%M:%S")

        if total_errors > 0:
            self.logger.debug(
                f"Read complete with {total_errors} error(s) "
                f"(plant={plant_errors}, inverter={inv_errors})"
            )

        return data

    # ================================================================
    # Low-Level Write Primitives
    # ================================================================

    @_locked
    def _write_single_register(self, register, value, slave=None, verify=True):
        """Write a single 16-bit register (function 0x06).

        verify=True (default) reads the register back ~150ms later and logs a
        WARNING if the inverter did not accept the value.  Some Sigenergy
        firmware revisions silently ignore writes to certain registers under
        specific EMS modes — verification catches this drift.  Set verify=False
        for high-frequency writes (e.g. solar overflow cap) where the extra
        Modbus round trip is unwelcome.
        """
        if slave is None:
            slave = self.plant_address
        if not self._connected:
            self.logger.error("Cannot write - not connected to inverter")
            return False
        self._throttle()
        try:
            result = self.client.write_register(address=register, value=value, device_id=slave)
            if result.isError():
                self.logger.error(f"Failed to write reg {register}={value} (slave {slave}): {result}")
                # Mark connection invalid so the next operation reconnects.
                # Without this, a failed write leaves the socket in zombie state
                # and subsequent writes silently fail.
                self._connected = False
                return False
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus write error reg {register} (slave {slave}): {e}")
            self._connected = False
            return False
        except Exception as e:
            self.logger.error(f"Unexpected write error reg {register} (slave {slave}): {e}")
            self._connected = False
            return False

        if verify:
            # Brief delay to let the inverter latch the new value before reading back.
            self._sleep(0.15)   # injected sleep — interruptible + mockable in tests
            try:
                readback = self._read_uint16(register, slave=slave)
            except Exception as e:
                self.logger.debug(f"Write-back verify read error for reg {register}: {e}")
                return True   # treat as success — write itself was accepted
            if readback is None:
                self.logger.debug(f"Write-back verify could not read reg {register}")
                return True
            if int(readback) != int(value):
                # A successful readback that disagrees is a hard fact — the
                # inverter rejected or clamped the write. Callers treat True as
                # "the register now holds this value", so returning True here
                # made safety-critical rejections invisible (v5.43 change).
                self.logger.warning(
                    f"Write-back mismatch: reg {register} wrote {value}, reads {readback} "
                    f"— inverter rejected or clamped the value"
                )
                return False
        return True

    @_locked
    def _write_uint32_registers(self, register, value, slave=None, verify=True):
        """Write a 32-bit unsigned value to two consecutive registers (function 0x10).

        verify=True (default) reads the pair back ~150ms later and logs a
        WARNING on mismatch.  See _write_single_register for rationale.
        """
        if slave is None:
            slave = self.plant_address
        if not self._connected:
            self.logger.error("Cannot write - not connected to inverter")
            return False
        self._throttle()
        high_word = (value >> 16) & 0xFFFF
        low_word  = value & 0xFFFF
        try:
            result = self.client.write_registers(
                address=register, values=[high_word, low_word], device_id=slave
            )
            if result.isError():
                self.logger.error(
                    f"Failed to write regs {register}-{register+1}={value} (slave {slave}): {result}"
                )
                # Mark connection invalid so the next operation reconnects.
                self._connected = False
                return False
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus write error regs {register}-{register+1} (slave {slave}): {e}")
            self._connected = False
            return False
        except Exception as e:
            self.logger.error(f"Unexpected write error regs {register}-{register+1} (slave {slave}): {e}")
            self._connected = False
            return False

        if verify:
            self._sleep(0.15)   # injected sleep — interruptible + mockable in tests
            try:
                readback = self._read_uint32(register, slave=slave)
            except Exception as e:
                self.logger.debug(f"Write-back verify read error for regs {register}: {e}")
                return True
            if readback is None:
                self.logger.debug(f"Write-back verify could not read regs {register}")
                return True
            if int(readback) != int(value):
                # See _write_single_register — a confirmed mismatch returns False
                # so callers know the safety write did not take (v5.43 change).
                self.logger.warning(
                    f"Write-back mismatch: regs {register}-{register+1} wrote {value}, "
                    f"reads {readback} — inverter rejected or clamped the value"
                )
                return False
        return True

    # ================================================================
    # Remote EMS Control
    # ================================================================

    def enable_remote_ems(self):
        """Enable Remote EMS control (register 40029 = 1)."""
        self.logger.info("Enabling Remote EMS control")
        success = self._write_single_register(HOLD_REMOTE_EMS_ENABLE, 1)
        if not success:
            self.logger.error("Failed to enable Remote EMS")
        return success

    def disable_remote_ems(self):
        """Disable Remote EMS - returns plant to local EMS control."""
        self.logger.info("Disabling Remote EMS - returning to local EMS")
        success = self._write_single_register(HOLD_REMOTE_EMS_ENABLE, 0)
        if not success:
            self.logger.error("Failed to disable Remote EMS")
        return success

    def set_remote_ems_mode(self, mode):
        """Set Remote EMS control mode (register 40031)."""
        mode_name = REMOTE_EMS_MODES.get(mode, f"Unknown ({mode})")
        if mode not in REMOTE_EMS_MODES:
            self.logger.error(f"Invalid Remote EMS mode: {mode}")
            return False
        self.logger.info(f"Setting Remote EMS mode: {mode_name} (0x{mode:02X})")
        success = self._write_single_register(HOLD_REMOTE_EMS_MODE, mode)
        if not success:
            self.logger.error(f"Failed to set Remote EMS mode: {mode_name}")
        return success

    def set_charge_limit(self, watts, quiet=False):
        """Set ESS max charging power (registers 40032-40033, watts).

        quiet=True suppresses the INFO log AND the read-back verification —
        used during solar overflow cap adjustments where the Manager summary
        already logs the change and the high-frequency call doesn't justify
        the extra Modbus round trip.
        """
        if watts < 0:
            self.logger.error(f"Invalid charge limit: {watts}W (must be >= 0)")
            return False
        if watts > MAX_POWER_LIMIT_W:
            self.logger.warning(f"Charge limit {watts}W exceeds sanity ceiling "
                                f"{MAX_POWER_LIMIT_W}W — clamping")
            watts = MAX_POWER_LIMIT_W
        if not quiet:
            self.logger.info(f"Setting ESS max charge limit: {watts}W")
        else:
            self.logger.debug(f"Setting ESS max charge limit: {watts}W")
        return self._write_uint32_registers(HOLD_ESS_MAX_CHARGE, watts, verify=not quiet)

    def set_discharge_limit(self, watts):
        """Set ESS max discharging power (registers 40034-40035, watts)."""
        if watts < 0:
            self.logger.error(f"Invalid discharge limit: {watts}W (must be >= 0)")
            return False
        if watts > MAX_POWER_LIMIT_W:
            self.logger.warning(f"Discharge limit {watts}W exceeds sanity ceiling "
                                f"{MAX_POWER_LIMIT_W}W — clamping")
            watts = MAX_POWER_LIMIT_W
        self.logger.info(f"Setting ESS max discharge limit: {watts}W")
        return self._write_uint32_registers(HOLD_ESS_MAX_DISCHARGE, watts)

    def set_export_limit(self, watts):
        """Set grid max export power limit (registers 40038-40039, watts).

        Global DNO export cap. Takes effect regardless of EMS mode.
        Requires grid sensor connected.

        Args:
            watts: Export limit in watts. Use 4000 for 4kW DNO limit.
        """
        if watts < 0:
            self.logger.error(f"Invalid export limit: {watts}W (must be >= 0)")
            return False
        if watts > MAX_POWER_LIMIT_W:
            self.logger.warning(f"Export limit {watts}W exceeds sanity ceiling "
                                f"{MAX_POWER_LIMIT_W}W — clamping")
            watts = MAX_POWER_LIMIT_W
        self.logger.info(f"Setting grid max export limit: {watts}W")
        success = self._write_uint32_registers(HOLD_GRID_MAX_EXPORT_LIMIT, watts)
        if not success:
            self.logger.error(f"Failed to set export limit to {watts}W")
        return success

    # ================================================================
    # Convenience Methods
    # ================================================================

    def force_charge(self, power_watts=10000, cutoff_soc=None):
        """Force charge battery from grid at specified power.

        Enables Remote EMS, sets Charge Grid First mode, sets power limit.

        cutoff_soc (optional): also write HOLD_ESS_CHARGE_CUTOFF (40047) as a
        HARDWARE backstop so a plugin crash or Modbus outage mid-import cannot
        leave the inverter grid-charging unbounded toward 100%. The plugin's
        software SOC compare remains the primary stop; callers pass a couple of
        percent of headroom above their target so the backstop only bites when
        the software stop is unreachable. Best-effort: a failed cutoff write
        logs a WARNING but does not fail the import (returning False here would
        leave mode 0x03 latched with the plugin believing no import started —
        strictly worse than the pre-backstop behaviour).
        """
        self.logger.info(f"Force charging from grid at {power_watts}W")
        if not self.enable_remote_ems():
            return False
        if not self.set_remote_ems_mode(0x03):
            return False
        if not self.set_charge_limit(power_watts):
            return False
        if cutoff_soc is not None:
            if not self.set_charge_cutoff(min(max(float(cutoff_soc), 0.0), 100.0)):
                self.logger.warning(
                    "Charge-cutoff backstop write failed — import runs without a "
                    "hardware SOC ceiling (software stop at target still active)"
                )
        self.logger.info(f"Force charge active: {power_watts}W from grid")
        return True

    def force_discharge(self, power_watts=4000):
        """Force discharge battery to grid at specified power.

        Caps TOTAL battery output at power_watts (house load + grid combined).
        Used for VPP and other operations where total battery output must be limited.
        For night export (where grid should receive the full export_watts regardless
        of house load), use night_export() instead.

        Enables Remote EMS, sets Discharge ESS First mode, sets power limit.
        """
        self.logger.info(f"Force discharging to grid at {power_watts}W")
        if not self.enable_remote_ems():
            return False
        if not self.set_remote_ems_mode(0x06):
            return False
        if not self.set_discharge_limit(power_watts):
            return False
        self.logger.info(f"Force discharge active: {power_watts}W to grid")
        return True

    def night_export(self, inverter_max_w=None):
        """Discharge battery to grid while also supplying house load.

        Sets Discharge ESS First mode (0x06) with HOLD_ESS_MAX_DISCHARGE at inverter
        maximum. The inverter's own DNO export cap (configured during commissioning)
        limits grid export automatically — no need to write HOLD_GRID_MAX_EXPORT_LIMIT.

        Battery discharges at (house_load + grid_export), up to inverter_max_w.
        Grid always receives its full export allocation regardless of home consumption.
        """
        inverter_max_w = inverter_max_w or self.inverter_max_w
        self.logger.info(
            f"Night export: mode 0x06, discharge limit {inverter_max_w}W "
            f"(inverter DNO cap enforces grid limit)"
        )
        if not self.enable_remote_ems():
            return False
        if not self.set_remote_ems_mode(0x06):
            return False
        if not self.set_discharge_limit(inverter_max_w):
            return False
        self.logger.info("Night export active: battery supplying house load + grid export")
        return True

    def daytime_export(self, inverter_max_w=None):
        """Discharge to grid PV-first, battery only covering the shortfall.

        Sets Discharge PV First mode (0x05) AND pins the charge limit to 0. The
        inverter's own commissioned DNO export cap limits grid flow (typically
        4 kW) — same as night_export, no need to write HOLD_GRID_MAX_EXPORT_LIMIT.

        WHY charge limit 0 (learned on hardware 15-Jun-2026): in mode 0x05 with
        the charge limit left open, when PV exceeds house load + the export cap
        the inverter greedily charges the battery with the surplus INSTEAD of
        exporting — grid sits near 0 and the paid dispatch is missed (observed
        20-60s of "charging, not exporting" at high PV). Pinning the charge
        limit to 0 removes that competing path, so the PV surplus is forced out
        to the grid up to the DNO cap immediately and stably.

        Behaviour with charge limit 0:
          - PV >= cap + house: grid exports at the DNO cap from PV, battery flat,
            any PV above (cap + house) is curtailed for the window.
          - PV  < cap + house: export = PV + battery shortfall (PV-first).
          - PV == 0: behaves exactly like night_export (battery supplies it all).
        This guarantees the full (paid) dispatch in all PV conditions while
        keeping the battery essentially flat (preserved) during daylight.
        Trade-off vs night_export (0x06): PV keeps running and the battery is
        not drained; the only cost is curtailing PV above the cap during the
        window (the export payment far outweighs the un-banked surplus, and the
        battery refills from solar after the event).

        ORDER MATTERS, and it used to be wrong. Mode 0x05 was committed FIRST and
        the charge limit pinned two writes later, which opens exactly the window
        this method exists to close: with 0x05 live and the charge limit still at
        inverter max (where set_self_consumption leaves it), high PV banks into
        the battery instead of going to grid. The mode register reads a perfectly
        correct 0x05 throughout, so nothing downstream can tell that a paid
        window is exporting nothing. The limits are now written BEFORE the mode
        commit — charge 0 while still in the previous mode costs at most a
        sub-second pause in battery charging, against a silently unpaid slice of
        a VPP window.
        """
        inverter_max_w = inverter_max_w or self.inverter_max_w
        self.logger.info(
            f"Daytime export: mode 0x05 (PV first), discharge limit {inverter_max_w}W, "
            f"charge limit 0 (force PV to grid; inverter DNO cap enforces grid limit)"
        )
        if not self.enable_remote_ems():
            return False
        if not self.set_charge_limit(0):
            return False
        if not self.set_discharge_limit(inverter_max_w):
            return False
        if not self.set_remote_ems_mode(0x05):
            return False
        self.logger.info("Daytime export active: PV forced to grid, battery covers any shortfall")
        return True

    def set_self_consumption(self):
        """Set Max Self Consumption mode via Remote EMS.

        HOLD_ESS_MAX_CHARGE (40032) and HOLD_ESS_MAX_DISCHARGE (40034) are
        persistent registers — their values survive mode changes on the inverter.
        A previous force_charge() or force_discharge() call leaves a stale limit
        that caps battery output even in self-consumption mode. Both are reset to
        the inverter maximum (self.inverter_max_w) here on every transition to
        self-consumption — that used to be a hardcoded 10000, correct only on a
        10 kW inverter and a silent discharge cap on any other.
        """
        self.logger.info("Setting Remote EMS: Max Self Consumption")
        if not self.enable_remote_ems():
            return False
        if not self.set_remote_ems_mode(0x02):
            return False
        # Reset both power limits — they persist across mode changes. A failed
        # reset means a stale force-charge/discharge cap may still throttle the
        # battery in self-consumption, so report it (the manager's verify pass
        # re-asserts the limits next cycle either way).
        ok_discharge = self.set_discharge_limit(self.inverter_max_w)
        ok_charge    = self.set_charge_limit(self.inverter_max_w)
        if not (ok_discharge and ok_charge):
            self.logger.error(
                f"Self Consumption set but limit reset failed "
                f"(discharge={'OK' if ok_discharge else 'FAILED'}, "
                f"charge={'OK' if ok_charge else 'FAILED'}) — verify pass will retry"
            )
            return False
        self.logger.info("Remote EMS: Max Self Consumption active (charge/discharge limits cleared)")
        return True

    def return_to_local(self):
        """Return inverter to its own local EMS control."""
        self.logger.info("Returning to local EMS control")
        return self.disable_remote_ems()

    def read_ems_mode(self):
        """Read current HOLD_REMOTE_EMS_MODE (40031). Returns int or None."""
        if not self._connected:
            return None
        raw = self._read_uint16(HOLD_REMOTE_EMS_MODE)
        if raw is None:
            return None
        self.logger.debug(f"EMS mode read: {REMOTE_EMS_MODES.get(raw, f'Unknown ({raw})')}")
        return raw

    def read_discharge_limit(self):
        """Read current HOLD_ESS_MAX_DISCHARGE (registers 40034-40035). Returns watts or None."""
        if not self._connected:
            return None
        raw = self._read_uint32(HOLD_ESS_MAX_DISCHARGE)
        if raw is None:
            return None
        self.logger.debug(f"Discharge limit read: {raw}W")
        return raw

    def read_charge_limit(self):
        """Read current HOLD_ESS_MAX_CHARGE (registers 40032-40033). Returns watts or None."""
        if not self._connected:
            return None
        raw = self._read_uint32(HOLD_ESS_MAX_CHARGE)
        if raw is None:
            return None
        self.logger.debug(f"Charge limit read: {raw}W")
        return raw

    def read_export_limit(self):
        """Read current HOLD_GRID_MAX_EXPORT_LIMIT (registers 40038-40039). Returns watts or None.

        The commissioned grid export cap — your DNO / G99 limit, set at
        commissioning. Reading it back lets a setup wizard pre-fill the export
        target from what the inverter is actually capped to, rather than asking
        the user to remember it.
        """
        if not self._connected:
            return None
        raw = self._read_uint32(HOLD_GRID_MAX_EXPORT_LIMIT)
        if raw is None:
            return None
        self.logger.debug(f"Export limit read: {raw}W")
        return raw

    # ================================================================
    # ESS SOC Limits (V2.6+ registers)
    # ================================================================

    def set_discharge_cutoff(self, soc_pct):
        """Set ESS minimum discharge SOC (register 40048).

        Global hardware limit - battery will not discharge below this SOC
        regardless of EMS mode. Used by VPP to protect post-event reserve.

        Args:
            soc_pct: Minimum SOC % (0.0 - 100.0)
        """
        if not (0.0 <= soc_pct <= 100.0):
            self.logger.error(f"Invalid discharge cutoff: {soc_pct}% (must be 0-100)")
            return False
        raw_value = int(round(soc_pct * 10))
        self.logger.info(f"Setting ESS discharge cutoff: {soc_pct:.1f}% (raw={raw_value})")
        success = self._write_single_register(HOLD_ESS_DISCHARGE_CUTOFF, raw_value)
        if not success:
            self.logger.error(f"Failed to set discharge cutoff to {soc_pct:.1f}%")
        return success

    def read_discharge_cutoff(self):
        """Read current ESS discharge cutoff SOC from register 40048.

        Returns:
            float: Discharge cutoff % or None on error.
        """
        if not self._connected:
            self.logger.warning("Cannot read discharge cutoff - not connected")
            return None
        # 40048 is a holding register - read with function 0x03
        raw = self._read_uint16(HOLD_ESS_DISCHARGE_CUTOFF)
        if raw is None:
            return None
        soc_pct = raw / 10.0
        self.logger.debug(f"Discharge cutoff: {soc_pct:.1f}% (raw={raw})")
        return soc_pct

    def set_charge_cutoff(self, soc_pct):
        """Set ESS maximum charge SOC (register 40047).

        HARDWARE-VERIFIED GLOBAL (supervised live test 02-07-2026): with the
        cutoff below current SOC, charging is blocked in Max Self Consumption
        (0x02: 5 kW PV charging collapsed to ~0 W within seconds, surplus went
        to export) AND in Charge Grid First (0x03: a commanded 2 kW grid
        charge held at 0 W battery power). So this register is a real crash
        backstop for force_charge, not just a self-consumption trim.
        Restore to 100.0 when the import/export ends to allow unrestricted
        charging.

        Args:
            soc_pct: Maximum charge SOC % (0.0 - 100.0)
        """
        if not (0.0 <= soc_pct <= 100.0):
            self.logger.error(f"Invalid charge cutoff: {soc_pct}% (must be 0-100)")
            return False
        raw_value = int(round(soc_pct * 10))
        self.logger.info(f"Setting ESS charge cutoff: {soc_pct:.1f}% (raw={raw_value})")
        success = self._write_single_register(HOLD_ESS_CHARGE_CUTOFF, raw_value)
        if not success:
            self.logger.error(f"Failed to set charge cutoff to {soc_pct:.1f}%")
        return success

    def read_charge_cutoff(self):
        """Read current ESS charge cutoff SOC from register 40047.

        Returns:
            float: Charge cutoff % or None on error.
        """
        if not self._connected:
            self.logger.warning("Cannot read charge cutoff - not connected")
            return None
        raw = self._read_uint16(HOLD_ESS_CHARGE_CUTOFF)
        if raw is None:
            return None
        soc_pct = raw / 10.0
        self.logger.debug(f"Charge cutoff: {soc_pct:.1f}% (raw={raw})")
        return soc_pct
