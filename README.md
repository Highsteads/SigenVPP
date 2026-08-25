# SigenVPP — standalone Axle VPP export controller for Sigenergy

Drives a Sigenergy hybrid inverter through an Axle Energy VPP export window over local
Modbus TCP. **No Indigo, no Claude, no cloud** beyond reading the Axle announcement. Plain
Python — runs on macOS, Windows, Linux, a Raspberry Pi, or a NAS (bare or in Docker).

It does one job well: when Axle announces an export event, it self-drives the inverter to
export your DNO-capped target to the grid, and:

- **plenty of sun** (PV surplus above the target) → exports the target from PV **and banks
  the rest into the battery** (no curtailment),
- **less sun, or none** → discharges the battery to top the export up to the target,

then restores Max Self Consumption afterwards. It re-decides every ~30 s from live PV, and
**always restores on exit** (Ctrl-C, SIGTERM, crash) so the battery is never left forced.

## Before you start

You'll need:

- **A Sigenergy with Modbus TCP enabled** — switched on in the Sigen app. Nothing here can
  connect until it is, so do this first.
- **The inverter's LAN IP** (a reserved/static DHCP lease is wise), on the same network as
  the machine you'll run this on.
- **An Axle VPP account and API token** — the Bearer token Axle's Home Assistant integration
  uses.
- **Python 3.9+** on the host (macOS / Windows / Linux / Pi / NAS).

## One controller per inverter

The Sigenergy takes one boss at a time. If you also run the **SigenEnergyManager Indigo
plugin**, do **not** run this against the same inverter at the same time — they'd fight over
Remote EMS. Either run this on a machine that does *not* also run the plugin's VPP, or
**disable the plugin** while testing this.

## Install

```bash
python3 -m venv venv && . venv/bin/activate        # Windows: venv\Scripts\activate
pip install pymodbus requests
```

## Set it up

```bash
python3 sigen_vpp.py --setup
```

The wizard asks for just two things — the **inverter IP** and your **Axle API token** — tests
the Modbus connection, reads your commissioned export cap off the inverter to pre-fill the
target, and writes `config.json` (locked to owner-only). Everything else is defaulted; edit
`config.json` later if you want. (`config.example.json` shows every field.)

Your Axle token is the Bearer token from your Axle VPP account — the same one Axle's Home
Assistant integration uses.

## Use it

```bash
python3 sigen_vpp.py --status        # next Axle event + live inverter (read-only)
python3 sigen_vpp.py --test-export --minutes 3   # drive now for 3 min, then restore  (PLUGIN OFF)
python3 sigen_vpp.py --run           # the daemon: live dashboard + poll Axle + drive each window
```

When `--run` is going, open the **web dashboard** at `http://<host>:8179` (port set in
`config.json`). It updates every couple of seconds and shows the live power flow
(Solar / Home / Grid / Battery / INV), the VPP state banner (Idle / event countdown /
Pre-charging / **Exporting**), the export-vs-target figure, sub-mode (bank vs discharge),
SOC, and the next event. It's the page you'd leave open to watch an event run.

- `--status` is safe to run any time for the Axle half; the inverter read opens a second
  Modbus connection, so only run it with the plugin disabled.
- `--test-export` and `--run` take control of the inverter — **plugin off**.

## Run it as a service

- **Linux / Raspberry Pi (systemd):** a unit running `python3 sigen_vpp.py --run` with
  `Restart=always`. The restore-on-exit handler covers clean stops; `Restart=always` covers
  crashes.
- **macOS:** a `launchd` plist with `KeepAlive`.
- **Docker (great for a NAS):** base off `python:3.12-slim`, `pip install pymodbus requests`,
  mount `config.json`, `CMD ["python", "sigen_vpp.py", "--run"]`. One image runs on a Pi, a
  Synology/QNAP, or any Linux host.

## Security

`config.json` holds your Axle token (and any Pushover keys). It is written `chmod 600` and
must never be committed — `.gitignore` covers it.

The web dashboard binds to **127.0.0.1 by default**, so it is reachable only from the machine
running the daemon. It has no authentication, and `/api/status` carries the whole system state
— battery SOC, grid flow, the VPP schedule — so widening `web.bind` to `0.0.0.0` publishes all
of that to anyone on your network. That is a deliberate choice to make, not a default to
inherit.

Never forward the port from your router. If you want the dashboard from elsewhere, put it
behind Tailscale or a Cloudflare Tunnel, neither of which needs an open port.

## What's here

| File | Role |
|------|------|
| `sigen_vpp.py` | CLI: `--setup` / `--status` / `--test-export` / `--run` |
| `vpp_controller.py` | the bank/discharge driver + restore (no Indigo) |
| `config.py` | load/save `config.json` + defaults |
| `web_dashboard.py` | stdlib live web UI (`/` page + `/api/status`), fed by the daemon's state |
| `sigenergy_modbus.py` | Modbus client — **byte-identical copy of the plugin master** |
| `axle_api.py` | Axle event poller — **byte-identical copy of the plugin master** |
| `tools/check_shared_modules.py` | drift check for those two copies |

The dashboard is self-contained (no JS/CSS dependencies) and decoupled — it just calls a
status provider, so it has no knowledge of the controller internals.

## Shared modules

Two files here are copies. Their master lives in the SigenEnergyManager Indigo plugin, and
this repo carries them byte for byte so a checksum can police them.

They have drifted twice. The first time it was caught by hand. The second time this repo's
Modbus client had fallen five versions behind and its Axle client two, and nothing had
noticed. So before every release:

```
python3 tools/check_shared_modules.py
```

It exits 0 when the copies match, 1 when they have drifted, and 2 when it could not read the
master at all — because a check that cannot run must never report success. Add `--sync` to
copy the master over the copies, then run the tests again.

Point it at a master somewhere else with `SIGEN_PLUGIN_SRC`.

Keeping the files identical is the whole trick. A change only one side needs still goes in
the master, or this collapses back into hand-syncing. The proper answer is a shared core
package that both the plugin and this daemon import, and that does not exist yet.
