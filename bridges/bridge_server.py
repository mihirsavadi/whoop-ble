#!/usr/bin/env python3
"""
bridge_server.py - WHOOP BLE bridge: turns an Android phone into a Bluetooth
radio proxy for this PC.

    phone (Chrome, Web Bluetooth) <-HTTPS+poll-> this server <-HTTP-> you/curl

The phone page connects to the WHOOP band, subscribes to every WHOOP
notification characteristic plus the standard heart rate, and forwards all
bytes to this server. The server can queue write commands back to the phone.
Because the phone is ALREADY BONDED with the band (the WHOOP app paired it at
the OS level), the locked custom service should be accessible without any
pairing dance.

Run on the PC (Windows Python from WSL, from the repo root):
    python.exe bridges/bridge_server.py        # https://<LAN-IP>:8443

On the phone:
    1. Force-stop the WHOOP app (it holds the band's only connection slot).
    2. Open https://<LAN-IP>:8443 - accept the self-signed cert warning.
    3. Tap CONNECT, pick the WHOOP band.

Control endpoints (for agents / curl):
    GET  /state                      phone connected? queue? last log id
    GET  /log?since=<id>             log entries after <id> (JSONL rows)
    GET  /help                       this list
    POST /cmd/frame/<hex>            queue a raw frame for the phone to write
    POST /cmd/<catalog-name>         build a frame from whoop_ble.py's catalog
                                     (?payload=<hex> to override, ?seq=<n>)
    POST /reset                      clear the command queue

Catalog command names are the ones from `whoop_ble.py commands` (get_hello,
toggle_realtime_hr, get_data_range, ...). The known-good hello is
`POST /cmd/get_hello` with seq 1.

Stdlib only; imports catalog/frame helpers from whoop_ble.py (which loads
bleak lazily, so no BLE stack is needed on the PC side).
"""

import argparse
import json
import os
import socket
import ssl
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# repo root = parent of bridges/ (whoop_ble.py lives there)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from whoop_ble import (  # noqa: E402
    COMMAND_CATALOG,
    HELLO_FRAME,
    build_v5_frame,
    describe_frame,
    parse_hr_measurement,
)

HR_MEAS_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
POLL_TIMEOUT = 15  # seconds a /poll request holds before returning empty


# ---------------------------------------------------------------------------
# Shared state between HTTP threads
# ---------------------------------------------------------------------------

class Bus:
    def __init__(self):
        self.lock = threading.RLock()   # reentrant: log_event() nests inside cv-held sections
        self.cv = threading.Condition(self.lock)
        self.queue = []            # frames waiting for the phone to write
        self.log = deque(maxlen=5000)
        self.log_id = 0
        self.phone_state = "never connected"  # last status POSTed by the page

    def push_cmd(self, frame_hex: str, note: str = ""):
        with self.cv:
            self.queue.append({"type": "write", "frame": frame_hex})
            self.cv.notify_all()
            self.log_event("queued", "", frame_hex, note)

    def take_cmd(self, timeout: float):
        with self.cv:
            self.cv.wait_for(lambda: self.queue, timeout=timeout)
            if self.queue:
                return self.queue.pop(0)
            return None

    def drop_queue(self):
        with self.lock:
            n = len(self.queue)
            self.queue.clear()
            return n

    def log_event(self, kind: str, char: str, hexdata: str, desc: str = ""):
        with self.lock:
            self.log_id += 1
            entry = {
                "id": self.log_id,
                "ts": time.strftime("%H:%M:%S"),
                "kind": kind,
                "char": char,
                "hex": hexdata,
                "desc": desc,
            }
            self.log.append(entry)
            print(json.dumps(entry), flush=True)
            return entry

    def log_since(self, since: int):
        with self.lock:
            return [e for e in self.log if e["id"] > since]

    def set_phone_state(self, state: str):
        with self.lock:
            self.phone_state = state


BUS = Bus()


# ---------------------------------------------------------------------------
# The Web Bluetooth phone page (embedded; served at GET /)
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WHOOP BLE Bridge</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 1.5rem; background: #111; color: #eee; }
  h1 { font-size: 1.2rem; }
  #status { padding: .6rem 1rem; border-radius: .5rem; background: #222; margin-bottom: 1rem; }
  button { padding: .8rem 1.5rem; font-size: 1rem; border: none; border-radius: .5rem;
           background: #4CAF50; color: #fff; }
  button:disabled { background: #555; }
  #log { font-family: ui-monospace, monospace; font-size: .72rem; white-space: pre-wrap;
         background: #1a1a1a; padding: .8rem; border-radius: .5rem; max-height: 60vh;
         overflow-y: auto; word-break: break-all; }
</style>
</head>
<body>
<h1>WHOOP BLE Bridge</h1>
<div id="status">idle</div>
<button id="btn" onclick="connect()">CONNECT</button>
<div id="log"></div>
<script>
const V5_SERVICE = 'fd4b0001-cce1-4033-93ce-002d5875f58a';
const GEN4_SERVICE = '61080001-8d6d-82b8-614a-1c8cb0f8dcc6';
const CMD_CHAR = 'fd4b0002-cce1-4033-93ce-002d5875f58a';
const GEN4_CMD_CHAR = '61080002-8d6d-82b8-614a-1c8cb0f8dcc6';
const NOTIFY_CHARS = ['fd4b0003-cce1-4033-93ce-002d5875f58a',
                      'fd4b0004-cce1-4033-93ce-002d5875f58a',
                      'fd4b0005-cce1-4033-93ce-002d5875f58a',
                      'fd4b0007-cce1-4033-93ce-002d5875f58a',
                      '61080003-8d6d-82b8-614a-1c8cb0f8dcc6',
                      '61080004-8d6d-82b8-614a-1c8cb0f8dcc6',
                      '61080005-8d6d-82b8-614a-1c8cb0f8dcc6',
                      '61080007-8d6d-82b8-614a-1c8cb0f8dcc6'];
const HR_MEAS = '00002a37-0000-1000-8000-00805f9b34fb';
const HR_SERVICE = '0000180d-0000-1000-8000-00805f9b34fb';

let device = null, cmdChar = null, running = false;

function setStatus(t) { document.getElementById('status').textContent = t; }
function logLine(t) {
  const el = document.getElementById('log');
  el.textContent = new Date().toLocaleTimeString() + '  ' + t + '\n' + el.textContent;
}

function toHex(dataView) {
  const b = new Uint8Array(dataView.buffer, dataView.byteOffset, dataView.byteLength);
  return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
}

async function post(path, obj) {
  try { await fetch(path, {method: 'POST', body: JSON.stringify(obj)}); }
  catch (e) { logLine('post failed: ' + e); }
}

function onNotify(e) {
  post('/event', {kind: 'notify', char: e.target.uuid, hex: toHex(e.target.value)});
}

async function connect() {
  if (!navigator.bluetooth) { setStatus('Web Bluetooth unavailable!'); return; }
  document.getElementById('btn').disabled = true;
  setStatus('requesting device...');
  try {
    device = await navigator.bluetooth.requestDevice({
      filters: [{namePrefix: 'WHOOP'}],
      optionalServices: [V5_SERVICE, GEN4_SERVICE, HR_SERVICE]
    });
    device.addEventListener('gattserverdisconnected', onDisconnect);
    setStatus('connecting to ' + (device.name || 'device') + '...');
    const server = await device.gatt.connect();

    let svc = null, cmdId = CMD_CHAR;
    for (const [svcId, cmdId_] of [[V5_SERVICE, CMD_CHAR], [GEN4_SERVICE, GEN4_CMD_CHAR]]) {
      try { svc = await server.getPrimaryService(svcId); cmdId = cmdId_; break; }
      catch (e) { /* try next */ }
    }
    if (!svc) throw new Error('no WHOOP service found');

    cmdChar = await svc.getCharacteristic(cmdId);
    let subbed = 0;
    for (const u of NOTIFY_CHARS) {
      try {
        const c = await svc.getCharacteristic(u);
        await c.startNotifications();
        c.addEventListener('characteristicvaluechanged', onNotify);
        subbed++;
      } catch (e) { logLine('subscribe failed ' + u.slice(0, 8) + ': ' + e.message); }
    }
    try {
      const hrs = await server.getPrimaryService(HR_SERVICE);
      const hrc = await hrs.getCharacteristic(HR_MEAS);
      await hrc.startNotifications();
      hrc.addEventListener('characteristicvaluechanged', onNotify);
      subbed++;
    } catch (e) { /* optional */ }

    running = true;
    setStatus('connected to ' + device.name + ' (' + subbed + ' notification chars)');
    post('/status', {state: 'connected', name: device.name, subscribed: subbed});
    pollLoop();
  } catch (e) {
    setStatus('failed: ' + e.message);
    logLine('error: ' + e);
    post('/status', {state: 'failed', error: String(e)});
    document.getElementById('btn').disabled = false;
  }
}

function onDisconnect() {
  running = false;
  setStatus('disconnected');
  logLine('gattserverdisconnected');
  post('/status', {state: 'disconnected'});
  document.getElementById('btn').disabled = false;
}

async function pollLoop() {
  while (running) {
    let r;
    try { r = await fetch('/poll'); }
    catch (e) { if (running) { await new Promise(res => setTimeout(res, 1000)); } continue; }
    const j = await r.json();
    if (j.cmd) {
      logLine('write: ' + j.cmd.frame);
      try {
        await cmdChar.writeValue(new Uint8Array(j.cmd.frame.match(/../g).map(h => parseInt(h, 16))));
        await post('/event', {kind: 'write_sent', char: cmdChar.uuid, hex: j.cmd.frame});
      } catch (e) {
        logLine('write FAILED: ' + e);
        await post('/event', {kind: 'write_failed', char: cmdChar.uuid,
                              hex: j.cmd.frame, desc: String(e)});
      }
    }
  }
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence default request logging
        pass

    # --- GET -------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._page()
        elif u.path == "/state":
            self._json({"phone": BUS.phone_state,
                        "queued": len(BUS.queue),
                        "last_log_id": BUS.log_id})
        elif u.path == "/log":
            since = int(q.get("since", ["0"])[0])
            entries = BUS.log_since(since)
            self._json({"entries": entries, "last_log_id": BUS.log_id})
        elif u.path == "/poll":
            cmd = BUS.take_cmd(POLL_TIMEOUT)
            self._json({"cmd": cmd})
        elif u.path == "/help":
            self._json({
                "get": ["/", "/state", "/log?since=N", "/poll", "/help"],
                "post": ["/cmd/frame/<hex>", "/cmd/<catalog-name>?payload=<hex>&seq=<n>",
                         "/event", "/status", "/reset"],
                "catalog_names": sorted(COMMAND_CATALOG.keys()),
            })
        else:
            self._json({"error": "not found"}, 404)

    # --- POST ------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}

        if u.path == "/event":
            kind = body.get("kind", "?")
            char = body.get("char", "")
            hexdata = body.get("hex", "")
            desc = body.get("desc", "")
            if kind == "notify" and not desc:
                desc = describe_frame(bytes.fromhex(hexdata)) if hexdata else ""
                if char.lower() == HR_MEAS_UUID and hexdata:
                    try:
                        bpm, rr = parse_hr_measurement(bytes.fromhex(hexdata))
                        desc = f"HR {bpm} bpm" + (f" RR={rr}" if rr else "")
                    except Exception:
                        pass
            BUS.log_event(kind, char, hexdata, desc)
            self._json({"ok": True})

        elif u.path == "/status":
            state = body.get("state", "?")
            BUS.set_phone_state(state)
            BUS.log_event("status", "", "", json.dumps(body))
            self._json({"ok": True})

        elif u.path == "/reset":
            n = BUS.drop_queue()
            BUS.log_event("reset", "", "", f"dropped {n} queued commands")
            self._json({"ok": True, "dropped": n})

        elif u.path.startswith("/cmd/frame/"):
            frame_hex = u.path[len("/cmd/frame/"):].replace(" ", "")
            try:
                bytes.fromhex(frame_hex)
            except ValueError:
                self._json({"error": "bad hex"}, 400)
                return
            BUS.push_cmd(frame_hex, note="raw frame")
            self._json({"ok": True, "queued": frame_hex})

        elif u.path.startswith("/cmd/"):
            name = u.path[len("/cmd/"):]
            entry = COMMAND_CATALOG.get(name)
            if not entry:
                self._json({"error": f"unknown command {name!r}; see /help"}, 404)
                return
            number, default_payload, _desc = entry
            payload_hex = q.get("payload", [None])[0] or default_payload
            if payload_hex is None:
                self._json({"error": "this command requires ?payload=<hex>"}, 400)
                return
            seq = int((q.get("seq") or [str(next_seq())])[0])
            frame = build_v5_frame(seq, number, bytes.fromhex(payload_hex))
            BUS.push_cmd(frame.hex(), note=f"{name} (#{number})")
            self._json({"ok": True, "queued": frame.hex(),
                        "name": name, "seq": seq, "payload": payload_hex})
        else:
            self._json({"error": "not found"}, 404)


_seq_counter = [0]
def next_seq():
    _seq_counter[0] = (_seq_counter[0] % 254) + 1
    return _seq_counter[0]


def primary_lan_ip() -> str:
    """Best-effort LAN IPv4 of this machine (the IP the phone should hit)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # no packets sent; just picks a route
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "<this-pc-lan-ip>"


def print_banner(ip: str, port: int, args):
    url = f"https://{ip}:{port}"
    line = "=" * 74
    print(line)
    print(" WHOOP BLE bridge - turns an Android phone into a BLE radio proxy")
    print(line)
    print(f" Listening on :{port} ({args.host})")
    print()
    print(" ON THE PHONE - open this exact URL in Chrome:")
    print(f"     {url}")
    print("   1. Force-stop the WHOOP app (it holds the band's only connection).")
    print("   2. Phone must be on the SAME WiFi/router as this PC.")
    print("   3. Accept the self-signed cert warning (Advanced -> Proceed).")
    print("   4. Tap CONNECT, pick the WHOOP band. Status should read")
    print("      'connected to WHOOP ... (N notification chars)'.")
    print()
    print(" IF THE PHONE SAYS 'Web Bluetooth API globally disabled':")
    print("   * Chrome:      chrome://flags/#enable-web-bluetooth -> Enabled -> relaunch")
    print("   * Samsung Int: internet://flags/#enable-web-bluetooth -> Enabled")
    print("   * Not Incognito; Bluetooth on; Chrome has 'Nearby devices' permission")
    print()
    print(" CONTROL FROM THIS PC (all catalog names via GET /help):")
    for ex in [
        f"curl.exe -sk {url}/state",
        f"curl.exe -sk -X POST {url}/cmd/get_hello",
        f'curl.exe -sk -X POST "{url}/cmd/toggle_realtime_hr"',
        f'curl.exe -sk -X POST "{url}/cmd/frame/<hexframe>"',
        f'curl.exe -sk "{url}/log?since=0"',
        f"curl.exe -sk -X POST {url}/reset",
    ]:
        print(f"     {ex}")
    print()
    print(f" OPTIONS: --port {port} --host {args.host}"
          f" --cert {args.cert} --key {args.key}")
    print(" If this PC's LAN IP changed, regenerate the cert so the warning")
    print(" shows the right SAN (browser proceeds either way):")
    print("   openssl req -x509 -newkey rsa:2048 -keyout certs/key.pem"
          " -out certs/cert.pem -days 825 -nodes \\")
    print("     -subj \"/CN=whoop-ble\" -addext \"subjectAltName=IP:<lan-ip>\"")
    print(line)
    print(" All band traffic is logged to stdout and GET /log.")
    print(line, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--cert", default=os.path.join(REPO_ROOT, "certs", "cert.pem"))
    parser.add_argument("--key", default=os.path.join(REPO_ROOT, "certs", "key.pem"))
    args = parser.parse_args()

    if not (os.path.exists(args.cert) and os.path.exists(args.key)):
        ip = primary_lan_ip()
        print("ERROR: HTTPS cert not found at:")
        print(f"       {args.cert} / {args.key}")
        print()
        print("Web Bluetooth requires HTTPS, so the server needs a self-signed")
        print("cert. Generate one with openssl (WSL has it; Git-Bash too):")
        print()
        print("  mkdir -p certs")
        print(f"  openssl req -x509 -newkey rsa:2048 -keyout {args.key} \\")
        print(f"      -out {args.cert} -days 825 -nodes \\")
        print(f"      -subj \"/CN=whoop-ble\" -addext \"subjectAltName=IP:{ip}\"")
        print()
        print("Then rerun. On the phone, accept the cert warning once.")
        print("certs/ is gitignored on purpose: key.pem is a PRIVATE KEY and")
        print("must never be committed — every user generates their own.")
        sys.exit(1)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(args.cert, args.key)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print_banner(primary_lan_ip(), args.port, args)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
