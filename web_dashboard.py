#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    web_dashboard.py
# Description: SigenVPP live web dashboard — stdlib-only HTTP server (no Flask, no
#              Indigo). Serves one self-contained page (/) that polls /api/status
#              and renders the live power flow, VPP state, next-event countdown and
#              export-vs-target. Fed by a status-provider callable, so it has no
#              knowledge of the controller internals.
# Author:      CliveS & Claude Opus 4.8
# Date:        15-06-2026
# Version:     0.1

import http.server
import json
import logging
import math
import socketserver
import threading


def _json_safe(obj):
    """Replace NaN/Infinity (which break browser JSON.parse) with None."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


class WebDashboard:
    """Threaded stdlib web server. `provider()` returns the live status dict."""

    def __init__(self, provider, host="127.0.0.1", port=8179,
                 logger=None, title="SigenVPP"):
        self.provider = provider
        self.host     = host
        self.port     = port
        self.title    = title
        self.log      = logger or logging.getLogger("SigenVPP.Web")
        self._httpd   = None
        self._thread  = None

    def start(self):
        provider, page = self.provider, _PAGE.replace("__TITLE__", self.title)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass  # silence per-request stderr spam

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/status":
                    try:
                        body = json.dumps(_json_safe(provider())).encode("utf-8")
                    except Exception as exc:
                        body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                    self._send(200, body, "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        # Loopback by default. This page and /api/status carry the whole system
        # state - SOC, grid flow, VPP schedule - and there is no authentication
        # here, so binding every interface would publish all of it to the LAN.
        # Widen it deliberately in config.json if you want it from another
        # machine, and put it behind Tailscale rather than a forwarded port.
        self._httpd = Server((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="SigenVPP-web", daemon=True)
        self._thread.start()
        self.log.info("Dashboard at http://%s:%d", self.host, self.port)

    def stop(self):
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass


# --- the page (self-contained: inline CSS + JS, polls /api/status every 2s) ---
_PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{--bg:#0f1220;--card:#1a1f33;--ink:#e8ecf6;--mut:#8b93ad;--line:#2a3150;
        --pv:#f6b73c;--home:#9b8cff;--grid:#4aa3ff;--batt:#37c98b;--bad:#ff5d6c;--ok:#37c98b;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:900px;margin:0 auto;padding:18px}
  header{display:flex;align-items:center;gap:10px;margin-bottom:14px}
  header h1{font-size:18px;margin:0;font-weight:650}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--mut)}
  .dot.ok{background:var(--ok)} .dot.bad{background:var(--bad)}
  .sub{color:var(--mut);font-size:13px;margin-left:auto}
  .banner{border-radius:14px;padding:14px 18px;margin-bottom:16px;font-weight:650;font-size:17px;
          background:var(--card);border:1px solid var(--line)}
  .banner.active{background:#13351f;border-color:#1f6f43;color:#7ff0b0}
  .banner.pre{background:#33260f;border-color:#7a5a1c;color:#ffd591}
  .banner.soon{background:#0f2740;border-color:#1d5c8f;color:#9bd0ff}
  .banner.off{background:#331417;border-color:#7a2630;color:#ff9aa4}
  .grid{display:grid;grid-template-columns:1.2fr 1fr;gap:16px}
  @media(max-width:640px){.grid{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 10px}
  .flow{position:relative;height:280px}
  .node{position:absolute;width:96px;height:96px;border-radius:50%;display:flex;flex-direction:column;
        align-items:center;justify-content:center;border:2px solid var(--line);background:#141829;transform:translate(-50%,-50%)}
  .node .lbl{font-size:11px;color:var(--mut)} .node .val{font-size:17px;font-weight:680;margin-top:2px}
  .ic{width:14px;height:14px;vertical-align:-2px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
  .node.solar{left:50%;top:16%;border-color:var(--pv)} .node.solar .val{color:var(--pv)}
  .node.home{left:17%;top:50%;border-color:var(--home)} .node.home .val{color:var(--home)}
  .node.grid{left:83%;top:50%;border-color:var(--grid)} .node.grid .val{color:var(--grid)}
  .node.batt{left:50%;top:84%;border-color:var(--batt)} .node.batt .val{color:var(--batt)}
  .node.inv{left:50%;top:50%;width:64px;height:64px;border-color:var(--mut);color:var(--mut);font-size:12px}
  .kv{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--line)}
  .kv:last-child{border-bottom:0} .kv .k{color:var(--mut)} .kv .v{font-weight:620}
  svg.legs{position:absolute;inset:0;width:100%;height:100%}
  .leg{stroke:var(--line);stroke-width:3}
  .leg.on{stroke-dasharray:6 6;animation:dash 1s linear infinite}
  @keyframes dash{to{stroke-dashoffset:-12}}
</style></head>
<body><div class="wrap">
  <header>
    <span id="dot" class="dot"></span>
    <h1>__TITLE__</h1>
    <span class="sub" id="updated">connecting…</span>
  </header>
  <div id="banner" class="banner">Loading…</div>
  <div class="grid">
    <div class="card">
      <h2>Live power flow</h2>
      <div class="flow">
        <svg class="legs" viewBox="0 0 100 100" preserveAspectRatio="none">
          <line id="leg-solar" class="leg" x1="50" y1="16" x2="50" y2="50"/>
          <line id="leg-home"  class="leg" x1="17" y1="50" x2="50" y2="50"/>
          <line id="leg-grid"  class="leg" x1="50" y1="50" x2="83" y2="50"/>
          <line id="leg-batt"  class="leg" x1="50" y1="50" x2="50" y2="84"/>
        </svg>
        <div class="node solar"><span class="lbl"><svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6l-1.4 1.4M7 17l-1.4 1.4"/></svg> Solar</span><span class="val" id="pv">–</span></div>
        <div class="node home"><span class="lbl"><svg class="ic" viewBox="0 0 24 24"><path d="M3 11l9 -8l9 8"/><path d="M5 10v10h14v-10"/><path d="M9 20v-6h6v6"/></svg> Home</span><span class="val" id="home">–</span></div>
        <div class="node grid"><span class="lbl"><svg class="ic" viewBox="0 0 24 24"><path d="M13 3v7h6l-8 11v-7h-6l8 -11"/></svg> Grid</span><span class="val" id="grid">–</span></div>
        <div class="node batt"><span class="lbl"><svg class="ic" viewBox="0 0 24 24"><rect x="3" y="8" width="15" height="9" rx="1.5"/><path d="M20 11v3M6.5 11v3M9.5 11v3M12.5 11v3"/></svg> Battery</span><span class="val" id="batt">–</span></div>
        <div class="node inv">INV</div>
      </div>
    </div>
    <div class="card">
      <h2>VPP</h2>
      <div class="kv"><span class="k">State</span><span class="v" id="phase">–</span></div>
      <div class="kv"><span class="k">Sub-mode</span><span class="v" id="submode">–</span></div>
      <div class="kv"><span class="k">Export target</span><span class="v" id="target">–</span></div>
      <div class="kv"><span class="k">Grid export now</span><span class="v" id="exp">–</span></div>
      <div class="kv"><span class="k">Bank charge cap</span><span class="v" id="cap">–</span></div>
      <div class="kv"><span class="k">Battery SOC</span><span class="v" id="soc">–</span></div>
      <h2 style="margin-top:16px">Next event</h2>
      <div class="kv"><span class="k">When</span><span class="v" id="ev-when">–</span></div>
      <div class="kv"><span class="k">Countdown</span><span class="v" id="ev-cd">–</span></div>
      <div class="kv"><span class="k">Axle API</span><span class="v" id="axle">–</span></div>
    </div>
  </div>
</div>
<script>
const kw = w => (w==null) ? "–" : (Math.abs(w)<10?"0.00":(w/1000).toFixed(2))+" kW";
function leg(id, w){ const e=document.getElementById(id); if(!e)return;
  e.classList.toggle("on", Math.abs(w||0) > 30); }
function hms(s){ if(s==null||s<0) return "–"; s=Math.floor(s);
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=s%60;
  return (h?h+"h ":"")+(m<10&&h?"0":"")+m+"m "+(sec<10?"0":"")+sec+"s"; }
async function tick(){
  try{
    const r = await fetch("/api/status",{cache:"no-store"}); const d = await r.json();
    const dot=document.getElementById("dot");
    dot.className="dot "+(d.modbus_connected?"ok":"bad");
    document.getElementById("updated").textContent = d.ok===false ? ("error: "+(d.error||"")) :
        ("updated "+(d.updated||"–"));
    const inv=d.inverter||{};
    document.getElementById("pv").textContent=kw(inv.pv_w);
    document.getElementById("home").textContent=kw(inv.home_w);
    document.getElementById("grid").textContent=kw(inv.grid_w);
    document.getElementById("batt").textContent=kw(inv.battery_w);
    leg("leg-solar",inv.pv_w); leg("leg-home",inv.home_w);
    leg("leg-grid",inv.grid_w); leg("leg-batt",inv.battery_w);
    document.getElementById("phase").textContent=(d.phase||"–");
    document.getElementById("submode").textContent=(d.submode||"–");
    document.getElementById("target").textContent=kw(d.target_w);
    document.getElementById("exp").textContent=(inv.grid_w!=null&&inv.grid_w<0)?kw(-inv.grid_w):"0.00 kW";
    document.getElementById("cap").textContent=(d.bank_cap_w!=null)?kw(d.bank_cap_w):"–";
    document.getElementById("soc").textContent=(inv.soc_pct!=null)?inv.soc_pct+" %":"–";
    document.getElementById("axle").textContent=d.axle_ok?"reachable":"unreachable";
    const ev=d.next_event;
    document.getElementById("ev-when").textContent= ev?(ev.local||ev.start):"none scheduled";
    document.getElementById("ev-cd").textContent= ev?(ev.active?"running":hms(ev.starts_in_s)):"–";
    // banner
    const b=document.getElementById("banner"); let cls="banner", txt="";
    if(!d.modbus_connected){cls+=" off"; txt="⚠ Inverter offline";}
    else if(d.phase==="active"){cls+=" active"; txt="● EXPORTING — "+(d.submode==="bank"?"banking surplus":"battery top-up")+" → "+kw(d.target_w);}
    else if(d.phase==="precharging"){cls+=" pre"; txt="◐ Pre-charging for event";}
    else if(ev&&!ev.active){cls+=" soon"; txt="▲ VPP event in "+hms(ev.starts_in_s);}
    else {txt="Idle — no event";}
    b.className=cls; b.textContent=txt;
  }catch(e){ document.getElementById("updated").textContent="no response"; }
}
tick(); setInterval(tick, 2000);
</script>
</body></html>"""
