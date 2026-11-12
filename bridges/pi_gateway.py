#!/usr/bin/env python3
"""
pi_gateway.py — WHOOP 5.0 BLE gateway for Linux (Raspberry Pi, live-USB
Ubuntu, any BlueZ box). Runs where the Bluetooth radio is — NOT on Windows.

Two subcommands:

  pair   Bond with the band through bluetoothctl with the full SMP exchange
         visible on the console — the diagnostic that Windows pairing never
         gave us. If this succeeds, the bond persists and `serve` gets full
         access to the locked WHOOP custom service.

  serve  Connect to the band, subscribe to every WHOOP notification
         characteristic + standard heart rate, and expose the same HTTP
         control surface as bridge_server.py / android-bridge:

           GET  /state  /log?since=N  /help
           POST /write {"frame":hex}  /cmd/frame/<hex>
           POST /cmd/<catalog-name>?payload=<hex>&seq=<n>   (frames built locally)
           POST /reset

         Plain HTTP on the LAN — no cert needed (no browser involved).
         Control it from the PC:  curl http://<pi-ip>:8443/state

Setup on the Linux box (Raspberry Pi OS / Ubuntu):
    sudo apt install -y bluez python3-pip
    pip install bleak            # system pip may need --break-system-packages
    # copy the whole whoop-ble/ repo — this file resolves whoop_ble.py at the
    # repo root automatically, wherever you run it from

Usage:
    python3 pi_gateway.py pair [--address XX:XX:XX:XX:XX:XX]
    python3 pi_gateway.py serve [--address ...] [--port 8443]
"""

import argparse
import asyncio
import json
import os
import subprocess
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
    CMD_CHAR_UUIDS,
    COMMAND_CATALOG,
    HELLO_FRAME,
    HR_MEAS_UUID,
    NOTIFY_CHAR_UUIDS,
    build_v5_frame,
    describe_frame,
    looks_like_whoop,
    parse_hr_measurement,
)


# ---------------------------------------------------------------------------
# Shared gateway state (HTTP threads <-> BLE asyncio thread)
# ---------------------------------------------------------------------------

class Gateway:
    def __init__(self):
        import threading as _t
        self.lock = _t.RLock()
        self.events = deque(maxlen=3000)
        self.event_seq = 0
        self.state = "starting"
        self.band = None
        self.connected = False
        self.write_queue = []          # hex frames waiting to be written
        self.write_asyncio_queue = None  # asyncio.Queue created in the BLE loop
        self._loop = None

    def set_state(self, state, band=None):
        with self.lock:
            self.state = state
            if band:
                self.band = band

    def log_event(self, kind, char="", hexdata="", desc=""):
        with self.lock:
            self.event_seq += 1
            entry = {"id": self.event_seq, "ts": time.strftime("%H:%M:%S"),
                     "kind": kind, "char": char, "hex": hexdata, "desc": desc}
            self.events.append(entry)
        print(json.dumps(entry), flush=True)
        return entry

    def enqueue_write(self, frame_hex):
        with self.lock:
            self.write_queue.append(frame_hex)
        q = self.write_asyncio_queue
        if q is not None:
            asyncio.run_coroutine_threadsafe(q.put(frame_hex), self._loop)
        self.log_event("queued", "", frame_hex)

    def state_json(self):
        with self.lock:
            return {"state": self.state, "connected": self.connected,
                    "band": self.band, "queued": len(self.write_queue),
                    "log_last_id": self.event_seq}

    def log_since(self, since):
        with self.lock:
            return [e for e in self.events if e["id"] > since]


GW = Gateway()


# ---------------------------------------------------------------------------
# BLE side (runs in a background thread with its own asyncio loop)
# ---------------------------------------------------------------------------

async def scan_for_whoop(timeout: float = 30.0):
    """Scan until a WHOOP appears; returns (address, name) or (None, None)."""
    from bleak import BleakScanner
    found = []

    def handler(device, adv):
        name = device.name or adv.local_name or ""
        if looks_like_whoop(name, adv.service_uuids or []):
            found.append((device.address, name))

    print(f"[scan] looking for a WHOOP for up to {timeout:.0f}s ...", flush=True)
    async with BleakScanner(detection_callback=handler):
        deadline = time.monotonic() + timeout
        while not found and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
    return found[0] if found else (None, None)


def find_char(client, uuids, prop=None):
    for svc in client.services:
        for ch in svc.characteristics:
            if str(ch.uuid).lower() in uuids:
                if prop is None or prop in ch.properties:
                    return ch
    return None


async def ble_main(args):
    from bleak import BleakClient
    GW._loop = asyncio.get_running_loop()

    address = args.address
    if not address:
        address, name = await scan_for_whoop()
        if not address:
            GW.set_state("no device found")
            GW.log_event("scan", "", "", "no WHOOP found (is the phone holding it?)")
            return
    GW.set_state("address known", band=address)

    while True:
        try:
            GW.set_state("connecting", band=address)
            GW.log_event("gatt", "", "", f"connecting to {address}")
            async with BleakClient(address) as client:
                with GW.lock:
                    GW.connected = True
                GW.log_event("gatt", "", "", f"connected, MTU {client.mtu_size}")

                cmd_char = find_char(client, CMD_CHAR_UUIDS, "write")
                auth_failed = False
                notify_chars = [c for c in
                               (find_char(client, {u}) for u in NOTIFY_CHAR_UUIDS) if c]
                for c in notify_chars:
                    try:
                        await client.start_notify(c, make_notify_handler())
                        GW.log_event("cccd", str(c.uuid), "", "subscribed")
                    except Exception as e:
                        if "Authentication" in str(e):
                            auth_failed = True
                        GW.log_event("cccd", str(c.uuid), "", f"FAILED: {e}")
                hr_char = find_char(client, {HR_MEAS_UUID}, "notify")
                if hr_char:
                    try:
                        await client.start_notify(hr_char, make_notify_handler())
                        GW.log_event("cccd", str(hr_char.uuid), "", "subscribed (HR)")
                    except Exception as e:
                        GW.log_event("cccd", str(hr_char.uuid), "", f"FAILED: {e}")

                if auth_failed or not notify_chars:
                    GW.set_state("auth blocked")
                    GW.log_event("gatt", "", "",
                                 "custom service refused (authentication) — run "
                                 "`pi_gateway.py pair` to bond this host with the band first")
                elif cmd_char is None:
                    GW.set_state("no cmd char")
                    GW.log_event("gatt", "", "", "no writable command characteristic")
                else:
                    GW.set_state("ready")
                    GW.log_event("gatt", "", "", "READY")

                GW.write_asyncio_queue = asyncio.Queue()
                # flush anything queued before the connection existed
                while GW.write_queue:
                    await GW.write_asyncio_queue.put(GW.write_queue.pop(0))
                while True:
                    hexframe = await GW.write_asyncio_queue.get()
                    if cmd_char is None:
                        GW.log_event("write_result", "", hexframe, "skipped: no cmd char")
                        continue
                    try:
                        await client.write_gatt_char(cmd_char, bytes.fromhex(hexframe),
                                                     response=True)
                        GW.log_event("write_result", str(cmd_char.uuid), hexframe, "ok")
                    except Exception as e:
                        GW.log_event("write_result", str(cmd_char.uuid), hexframe,
                                     f"FAILED: {e}")
        except Exception as e:
            GW.log_event("gatt", "", "", f"connection error: {e}")
        finally:
            with GW.lock:
                GW.connected = False
                GW.write_asyncio_queue = None
        GW.set_state("reconnecting")
        GW.log_event("gatt", "", "", "reconnecting in 5s")
        await asyncio.sleep(5)


def make_notify_handler():
    def handler(char, data: bytes):
        hexdata = data.hex()
        desc = describe_frame(data)
        if str(char.uuid).lower() == HR_MEAS_UUID:
            try:
                bpm, rr = parse_hr_measurement(data)
                desc = f"HR {bpm} bpm" + (f" RR={rr}" if rr else "")
            except Exception:
                pass
        GW.log_event("notify", str(char.uuid), hexdata, desc)
    return handler


# ---------------------------------------------------------------------------
# HTTP control surface (main thread)
# ---------------------------------------------------------------------------

_seq = [0]
def next_seq():
    _seq[0] = (_seq[0] % 254) + 1
    return _seq[0]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/state":
            self._json(GW.state_json())
        elif u.path == "/log":
            self._json({"entries": GW.log_since(int(q.get("since", ["0"])[0])),
                        "last_log_id": GW.event_seq})
        elif u.path == "/help":
            self._json({
                "get": ["/state", "/log?since=N", "/help"],
                "post": ["/write", "/cmd/frame/<hex>", "/cmd/hello", "/reset",
                         "/cmd/<catalog-name>?payload=<hex>&seq=<n>"],
                "catalog_names": sorted(COMMAND_CATALOG.keys()),
                "note": "runs on the Linux box itself; frames built locally",
            })
        else:
            self._json({"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}

        if u.path == "/write":
            hexframe = body.get("frame", "")
            if not hexframe:
                self._json({"error": 'body needs {"frame":"<hex>"}'})
                return
            GW.enqueue_write(hexframe)
            self._json({"ok": True, "queued": hexframe})
        elif u.path == "/cmd/hello":
            GW.enqueue_write(HELLO_FRAME.hex())
            self._json({"ok": True, "queued": HELLO_FRAME.hex()})
        elif u.path.startswith("/cmd/frame/"):
            hexframe = u.path[len("/cmd/frame/"):]
            try:
                bytes.fromhex(hexframe)
            except ValueError:
                self._json({"error": "bad hex"})
                return
            GW.enqueue_write(hexframe)
            self._json({"ok": True, "queued": hexframe})
        elif u.path.startswith("/cmd/"):
            name = u.path[len("/cmd/"):]
            entry = COMMAND_CATALOG.get(name)
            if not entry:
                self._json({"error": f"unknown command {name!r}; see /help"})
                return
            number, default_payload, _ = entry
            payload_hex = q.get("payload", [None])[0] or default_payload
            if payload_hex is None:
                self._json({"error": "requires ?payload=<hex>"})
                return
            seq = int((q.get("seq") or [str(next_seq())])[0])
            frame = build_v5_frame(seq, number, bytes.fromhex(payload_hex))
            GW.enqueue_write(frame.hex())
            self._json({"ok": True, "queued": frame.hex(), "name": name, "seq": seq})
        elif u.path == "/reset":
            n = len(GW.write_queue)
            GW.write_queue.clear()
            if GW.write_asyncio_queue is not None:
                while not GW.write_asyncio_queue.empty():
                    GW.write_asyncio_queue.get_nowait()
            self._json({"ok": True, "dropped": n})
        else:
            self._json({"error": "not found"})


def cmd_serve(args):
    threading.Thread(target=lambda: asyncio.run(ble_main(args)), daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"WHOOP gateway control: http://0.0.0.0:{args.port}/  "
          f"(from the PC: curl http://<this-box-ip>:{args.port}/state)")
    httpd.serve_forever()


# ---------------------------------------------------------------------------
# pair: BlueZ bonding with visible SMP exchange
# ---------------------------------------------------------------------------

BLUETOOTHCTL_SCRIPT = [
    ("power on", 1.0),
    ("agent NoInputNoOutput", 0.5),
    ("default-agent", 0.5),
    # NOTE: the band must be advertising (phone Bluetooth off / WHOOP app stopped)
    ("scan on", 8.0),
    ("scan off", 1.0),
    # connect first (some devices only pair while connected), then pair
    ("connect {mac}", 12.0),
    ("pair {mac}", 15.0),
    ("trust {mac}", 1.0),
    ("info {mac}", 1.0),
    ("disconnect {mac}", 2.0),
    ("exit", 0.5),
]


def cmd_pair(args):
    mac = args.address
    if not mac:
        print("[pair] no --address given; scanning briefly for the band ...")
        mac, name = asyncio.run(scan_for_whoop(25))
        if not mac:
            print("[pair] no WHOOP found while scanning. Make sure the band is")
            print("       advertising: phone Bluetooth OFF / WHOOP app force-stopped.")
            return 1
        print(f"[pair] found {name!r} @ {mac}")
    else:
        mac = mac.upper()

    print(f"[pair] running bluetoothctl pairing dance for {mac}")
    print("[pair] watch for the SMP lines: 'Pairing ...', 'Confirm passkey',")
    print("       'Failed to pair', or nothing at all (band ignoring the request).")
    print("-" * 70)

    proc = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    output_lines = []

    def reader():
        for line in proc.stdout:
            line = line.rstrip()
            output_lines.append(line)
            print(f"  | {line}", flush=True)

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    try:
        for cmd, delay in BLUETOOTHCTL_SCRIPT:
            proc.stdin.write(cmd.format(mac=mac) + "\n")
            proc.stdin.flush()
            time.sleep(delay)
        proc.stdin.close()
    except Exception as e:
        print(f"[pair] error driving bluetoothctl: {e}")
    rt.join(timeout=5)
    proc.terminate()

    print("-" * 70)
    info = "\n".join(output_lines)
    paired = any("Paired: yes" in l for l in output_lines)
    if paired:
        print(f"[pair] SUCCESS: BlueZ reports Paired: yes for {mac}")
        print("[pair] The bond persists. Now run:")
        print(f"          python3 pi_gateway.py serve --address {mac}")
        print("[pair] and the locked WHOOP custom service should open up.")
        return 0
    print(f"[pair] NOT PAIRED. Full log above — the interesting lines are whatever")
    print("       appeared right after 'pair': that's the answer to WHY Windows")
    print("       failed too (band refusing new bonds vs. broken just-works flow).")
    print("[pair] BlueZ device record (for the README):")
    for l in output_lines:
        if any(k in l for k in ("Paired", "Trusted", "UUID", "Modalias", "ManufacturerKey")):
            print(f"  | {l}")
    return 1


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="WHOOP BLE gateway for Linux/BlueZ. Radio must be local.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pair", help="bond with the band via bluetoothctl (SMP visible)")
    p.add_argument("--address", default=None, help="band MAC (scanned if omitted)")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser("serve", help="connect + subscribe + HTTP control surface")
    p.add_argument("--address", default=None, help="band MAC (scanned if omitted)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8443)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
