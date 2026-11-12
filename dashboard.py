#!/usr/bin/env python3
"""
dashboard.py — browser dashboard for any WHOOP bridge.

    python.exe whoop_ble.py dashboard            # or: python.exe dashboard.py
    → http://localhost:8000

A stdlib-only web app served on the PC. It does NOT talk Bluetooth — it is
a frontend to whichever bridge is alive (phone app / web bridge / Linux
gateway), defaulting to http://localhost:8443. All frame building and
decoding happen here, PC-side, using whoop_ble.py.

Panels:
  * Live stream      — every bridge event, decoded (events get NAMES:
                       DOUBLE_TAP, WRIST_ON, HAPTICS_FIRED, ...; command
                       responses get result codes + haptics statuses)
  * Command console  — the 29-command catalog as buttons, grouped; anything
                       state-changing asks for confirmation
  * Haptics lab      — waveform-slot composer → set_alarm_time + run_alarm
  * STOP ALL         — big red button: full stopPhysiologyCapture sequence
  * Historical grab  — walks the page-pointer sync dance and lets you
                       download what the band streamed

Name tables (event names, result codes, haptics statuses, packet domains)
are from the Goose project's Rust core (Rust/core/src/protocol.rs and
GooseBLEClient+Parsing.swift) — see README credits.
"""

import argparse
import asyncio
import json
import os
import ssl
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)
from whoop_ble import (  # noqa: E402
    BATTERY_LEVEL_UUID,
    CMD_CHAR_UUIDS,
    COMMAND_CATALOG,
    DEVICE_INFO_UUIDS,
    HELLO_FRAME,
    HR_MEAS_UUID,
    NOTIFY_CHAR_UUIDS,
    build_v5_frame,
    find_whoop,
    parse_hr_measurement,
)

# ---------------------------------------------------------------------------
# Name tables (from Goose: protocol.rs / Parsing.swift)
# ---------------------------------------------------------------------------

RESULT_NAMES = {0: "FAILURE", 1: "SUCCESS", 2: "PENDING", 3: "UNSUPPORTED"}

HAPTICS_NAMES = {
    0: "UNKNOWN", 1: "VALID_PATTERN", 2: "INVALID_EFFECT", 3: "INVALID_LOOPS",
    4: "INVALID_DURATION", 5: "SUCCESSFUL_PLAY", 6: "HAPTICS_FAILURE",
    7: "HAPTICS_TIMEOUT", 8: "HAPTICS_BUSY", 9: "HAPTICS_STOPPED",
    10: "INVALID_ALARM_TIME", 11: "INVALID_ALARM_ID",
}

EVENT_NAMES = {
    0: "UNDEFINED", 1: "ERROR", 2: "CONSOLE_OUTPUT", 3: "BATTERY_LEVEL",
    4: "SYSTEM_CONTROL", 7: "CHARGING_ON", 8: "CHARGING_OFF",
    9: "WRIST_ON", 10: "WRIST_OFF", 11: "BLE_CONNECTION_UP",
    12: "BLE_CONNECTION_DOWN", 13: "RTC_LOST", 14: "DOUBLE_TAP", 15: "BOOT",
    16: "SET_RTC", 17: "TEMPERATURE_LEVEL", 18: "PAIRING_MODE",
    28: "FLASH_INIT_COMPLETE", 29: "STRAP_CONDITION_REPORT",
    33: "BLE_REALTIME_HR_ON", 34: "BLE_REALTIME_HR_OFF",
    56: "STRAP_DRIVEN_ALARM_SET", 57: "STRAP_DRIVEN_ALARM_EXECUTED",
    58: "APP_DRIVEN_ALARM_EXECUTED", 59: "STRAP_DRIVEN_ALARM_DISABLED",
    60: "HAPTICS_FIRED", 63: "EXTENDED_BATTERY_INFORMATION",
    96: "HIGH_FREQ_SYNC_PROMPT", 97: "HIGH_FREQ_SYNC_ENABLED",
    98: "HIGH_FREQ_SYNC_DISABLED", 100: "HAPTICS_TERMINATED",
    109: "BATTERY_PACK_INFO", 123: "GENERIC_FIRMWARE_EVENT",
}

TYPE_NAMES = {
    35: "command", 36: "command_response", 37: "puffin_command",
    38: "puffin_command_response", 40: "realtime_data", 43: "realtime_raw",
    47: "historical_data", 48: "event", 49: "metadata", 50: "console_logs",
    51: "realtime_imu", 52: "historical_imu", 53: "relative_puffin_events",
    54: "puffin_events_from_strap", 55: "battery_pack_console_logs",
    56: "puffin_metadata",
}

DOMAIN_NAMES = {
    7: "legacy_raw_or_research_counted", 9: "normal_history", 10: "raw_motion",
    11: "raw_stream_counted", 12: "normal_history", 16: "raw_ecg_labrador",
    17: "r17_optical_filtered", 18: "normal_history", 19: "research_packet",
    20: "raw_or_research_counted", 21: "raw_motion", 22: "research_packet",
    24: "normal_history", 25: "pulse_information", 26: "pulse_information",
}

META_NAMES = {1: "HistoryStart", 2: "HistoryEnd", 3: "HistoryComplete"}

# Goose stopPhysiologyCapture — everything OFF, in order.
STOP_SEQUENCE = [
    (153, "0100"), (108, "0100"), (107, "0100"), (154, "0100"),
    (106, "0100"), (63, "00"), (3, "00"),
]


def u16le(b, off):
    return struct.unpack_from("<H", b, off)[0] if len(b) >= off + 2 else None


def u32le(b, off):
    return struct.unpack_from("<I", b, off)[0] if len(b) >= off + 4 else None


# ---------------------------------------------------------------------------
# Bridge proxy
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.lock = threading.Lock()
        self.seq = 0

    def _fetch(self, full_url, timeout, data=None):
        req = Request(full_url, data=data, method="POST" if data else "GET")
        if full_url.startswith("https"):
            # self-signed cert on the web bridge — verify nothing
            return urlopen(req, timeout=timeout,
                           context=ssl._create_unverified_context())
        return urlopen(req, timeout=timeout)

    def _candidates(self):
        """As given, then scheme-flipped: the phone app speaks plain HTTP and
        the web bridge speaks HTTPS — often the same port, so auto-detect."""
        yield self.url
        if self.url.startswith("http://"):
            yield "https://" + self.url[len("http://"):]
        elif self.url.startswith("https://"):
            yield "http://" + self.url[len("https://"):]

    def probe(self):
        """Health check; adopts whichever scheme answers. Raises if none do."""
        last = None
        for base in self._candidates():
            try:
                with self._fetch(base + "/state", 3) as r:
                    self.url = base
                    return json.loads(r.read())
            except Exception as ex:
                last = ex
        raise last

    def get(self, path):
        with self._fetch(self.url + path, timeout=10) as r:
            return json.loads(r.read())

    def post(self, path, obj=None):
        data = json.dumps(obj or {}).encode()
        with self._fetch(self.url + path, timeout=10, data=data) as r:
            return json.loads(r.read())

    def next_seq(self):
        with self.lock:
            self.seq = self.seq % 250 + 1
            return self.seq

    def send_command(self, name, payload_hex=None):
        entry = COMMAND_CATALOG.get(name)
        if not entry:
            return {"error": f"unknown command {name!r}"}
        number, default_payload, _ = entry
        payload_hex = payload_hex if payload_hex is not None else default_payload
        if payload_hex is None:
            return {"error": "payload required"}
        seq = self.next_seq()
        frame = build_v5_frame(seq, number, bytes.fromhex(payload_hex))
        r = self.post("/write", {"frame": frame.hex()})
        if isinstance(r, dict):
            r.update({"ok": True, "name": name, "seq": seq, "frame": frame.hex()})
        return r

    def write_raw(self, hexframe):
        return self.post("/write", {"frame": hexframe})


BRIDGE = None  # set in main(): Bridge (HTTP to a bridge) or DirectBridge (local bleak)
MODE = "?"    # "bridge" | "direct"


# ---------------------------------------------------------------------------
# Direct mode: the dashboard IS the bridge (bleak on the local radio).
# No bond on this machine → public GATT only: scan/connect, device info,
# battery, and honest Insufficient-Authentication errors for anything locked.
# ---------------------------------------------------------------------------

class DirectBridge:
    """Duck-types Bridge so every dashboard feature routes the same way.
    In direct mode the catalog is locked (no bond); public reads happen
    automatically on connect and stream into the log like any bridge."""

    def __init__(self):
        self.lock = threading.RLock()
        self.events = deque(maxlen=3000)
        self.event_seq = 0
        self.state_str = "starting"
        self.band = None
        self.connected = False
        self.write_queue = []
        self.seq = 0
        self.thread = None
        self._stop = False
        self.info = {}          # public reads: label -> value

    # -- bridge-compatible interface --------------------------------------
    def get(self, path):
        if path == "/state" or path.startswith("/state"):
            with self.lock:
                return {"connected": self.connected, "ready": self.connected,
                        "band": self.band, "queued": len(self.write_queue),
                        "state": self.state_str, "log_last_id": self.event_seq,
                        "mode": "direct", "info": dict(self.info)}
        if path.startswith("/log"):
            q = parse_qs(urlparse(path).query)
            since = int(q.get("since", ["0"])[0])
            with self.lock:
                entries = [e for e in self.events if e["id"] > since]
                return {"entries": entries, "last_log_id": self.event_seq}
        return {"error": f"direct mode: no such endpoint {path}"}

    def post(self, path, obj=None):
        obj = obj or {}
        if path == "/write":
            frame = obj.get("frame", "")
            if not frame:
                return {"error": "frame hex required"}
            with self.lock:
                self.write_queue.append(frame)
            self.log_event("queued", "", frame, "direct mode: will fail without a bond")
            return {"ok": True, "queued": frame}
        return {"error": f"direct mode: no such endpoint {path}"}

    def next_seq(self):
        with self.lock:
            self.seq = self.seq % 250 + 1
            return self.seq

    def send_command(self, name, payload_hex=None):
        entry = COMMAND_CATALOG.get(name)
        if not entry:
            return {"error": f"unknown command {name!r}"}
        number, default_payload, _ = entry
        payload_hex = payload_hex if payload_hex is not None else default_payload
        if payload_hex is None:
            return {"error": "payload required"}
        seq = self.next_seq()
        frame = build_v5_frame(seq, number, bytes.fromhex(payload_hex))
        r = self.post("/write", {"frame": frame.hex()})
        r.update({"name": name, "seq": seq, "frame": frame.hex()})
        return r

    def write_raw(self, hexframe):
        return self.post("/write", {"frame": hexframe})

    # -- internals ---------------------------------------------------------
    def set_state(self, s):
        with self.lock:
            self.state_str = s

    def log_event(self, kind, char="", hexdata="", desc=""):
        with self.lock:
            self.event_seq += 1
            entry = {"id": self.event_seq, "ts": time.strftime("%H:%M:%S"),
                     "kind": kind, "char": char, "hex": hexdata, "desc": desc}
            self.events.append(entry)
        print(json.dumps(entry), flush=True)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop = False
        self.thread = threading.Thread(target=lambda: asyncio.run(self._ble_main()),
                                       daemon=True)
        self.thread.start()

    def stop(self):
        self._stop = True   # picked up within ~1 s (or after the current scan)
        self.log_event("scan", "", "", "direct backend stopping (bridge takes over)")

    async def _ble_main(self):
        from bleak import BleakClient
        while not self._stop:
            try:
                self.set_state("scanning (make sure the WHOOP app isn't holding the band)")
                self.log_event("scan", "", "", "direct BLE: scanning for a WHOOP")
                device, _adv = await find_whoop(25)
                if not device:
                    self.set_state("no band found — rescanning")
                    await asyncio.sleep(8)
                    continue
                with self.lock:
                    self.band = device.name or device.address
                self.set_state("connecting")
                self.log_event("gatt", "", "", f"connecting to {device.address}")
                async with BleakClient(device) as client:
                    with self.lock:
                        self.connected = True
                    self.set_state("connected (no bond — public services only)")
                    self.log_event("gatt", "", "",
                                   f"connected, MTU {client.mtu_size} — direct mode, no bond")
                    await self._read_public(client)
                    await self._subscribe_public(client)
                    await self._attempt_locked(client)
                    while not self._stop:
                        await asyncio.sleep(1)
                        while True:
                            with self.lock:
                                frame = self.write_queue.pop(0) if self.write_queue else None
                            if frame is None:
                                break
                            self.log_event("write_attempt", "", frame,
                                           "direct mode: no bond, expecting refusal")
            except Exception as ex:
                self.log_event("gatt", "", "", f"error: {ex}")
            finally:
                with self.lock:
                    self.connected = False
            if self._stop:
                return
            self.set_state("reconnecting")
            await asyncio.sleep(5)

    async def _read_public(self, client):
        for svc in client.services:
            for char in svc.characteristics:
                u = str(char.uuid).lower()
                if u in DEVICE_INFO_UUIDS and "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char)
                        text = val.decode(errors="replace").rstrip(chr(0))
                        with self.lock:
                            self.info[DEVICE_INFO_UUIDS[u]] = text
                        self.log_event("info", u, val.hex(),
                                       f"{DEVICE_INFO_UUIDS[u]} = {text!r}")
                    except Exception as ex:
                        self.log_event("info", u, "", f"read failed: {ex}")
                elif u == BATTERY_LEVEL_UUID and "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char)
                        with self.lock:
                            self.info["battery"] = f"{val[0]}%"
                        self.log_event("info", u, val.hex(), f"battery {val[0]}%")
                    except Exception as ex:
                        self.log_event("info", u, "", f"read failed: {ex}")

    async def _subscribe_public(self, client):
        for svc in client.services:
            for char in svc.characteristics:
                u = str(char.uuid).lower()
                if u in (HR_MEAS_UUID, BATTERY_LEVEL_UUID) and "notify" in char.properties:
                    try:
                        await client.start_notify(char, self._on_notify)
                        self.log_event("cccd", u, "", "subscribed (public)")
                    except Exception as ex:
                        self.log_event("cccd", u, "", f"subscribe failed: {ex}")

    def _on_notify(self, char, data: bytes):
        self.log_event("notify", str(char.uuid), data.hex())

    async def _attempt_locked(self, client):
        """Touch the locked service on purpose so the dashboard can show
        the exact, honest reason the catalog is disabled in direct mode."""
        for svc in client.services:
            for char in svc.characteristics:
                if str(char.uuid).lower() in NOTIFY_CHAR_UUIDS:
                    try:
                        await client.start_notify(char, self._on_notify)
                        self.log_event("cccd", str(char.uuid), "",
                                       "UNEXPECTED: custom char opened without a bond!")
                    except Exception as ex:
                        self.log_event("cccd", str(char.uuid), "",
                                       f"locked (expected): {ex}")
                        return  # one honest sample is enough


# ---------------------------------------------------------------------------
# Frame enrichment (server-side decoding for the live panel)
# ---------------------------------------------------------------------------

def enrich_entry(e):
    """Add decoded fields to a bridge log entry (notify kinds with hex)."""
    if e.get("kind") != "notify" or not e.get("hex"):
        return e
    try:
        b = bytes.fromhex(e["hex"])
    except ValueError:
        return e
    if len(b) < 9 or b[0] != 0xAA:
        return e
    ptype = b[8]
    e["type"] = ptype
    e["type_name"] = TYPE_NAMES.get(ptype, f"type_{ptype}")

    if ptype == 36 and len(b) >= 13:            # command response
        e["resp_cmd"] = b[10]
        e["echo_seq"] = b[11]
        e["result"] = RESULT_NAMES.get(b[12], f"code_{b[12]}")
        if b[10] in (66, 68) and len(b) >= 15:   # alarm family: haptics status
            e["haptics"] = HAPTICS_NAMES.get(b[14], f"status_{b[14]}")
    elif ptype == 48 and len(b) >= 16:          # strap event
        eid = u16le(b, 10)
        e["event_id"] = eid
        e["event_name"] = EVENT_NAMES.get(eid, f"event_{eid}")
        ts = u32le(b, 12)
        if ts:
            e["event_unix"] = ts
    elif ptype == 49 and len(b) >= 11:          # sync metadata
        kind = b[10]   # single byte for type 49 (u16 at [10:12] only for type 56)
        e["meta"] = META_NAMES.get(kind, f"meta_{kind}")
        if kind == 2 and len(b) >= 29:
            e["end_token"] = b[21:29].hex()   # 8-byte ack token — echo verbatim
    elif ptype == 56 and len(b) >= 12:          # puffin metadata: u16 kind
        kind = u16le(b, 10)
        e["meta"] = META_NAMES.get(kind, f"meta_{kind}")
    elif ptype == 40 and len(b) >= 18:            # live HR + R-R (NOOP layout, verified live)
        e["packet_unix"] = u32le(b, 10)
        e["hr"] = b[16]
        n = b[17]
        rr = [round(struct.unpack_from("<H", b, 18 + 2 * i)[0] * 1000 / 1024)
              for i in range(n) if 18 + 2 * i + 2 <= len(b) - 4]
        if rr:
            e["rr_ms"] = rr
    elif ptype == 43 and len(b) > 10:             # live sensor data; layout selector at 9
        k = b[9]
        e["packet_k"] = k
        e["domain"] = {16: "raw ECG (R16)", 17: "filtered ECG (R17)"}.get(
            k, DOMAIN_NAMES.get(k))
    elif ptype in (47, 51, 52) and len(b) >= 16:  # historical / IMU packets
        k = b[10]
        e["packet_k"] = k
        e["domain"] = DOMAIN_NAMES.get(k)
        e["page"] = u32le(b, 12)
        e["packet_unix"] = u32le(b, 16)
        if ptype == 47 and k == 18 and len(b) >= 75:   # R18 biometric summary
            e["r18"] = True
            e["packet_unix"] = u32le(b, 15) or e["packet_unix"]
            e["hr"] = b[22] if b[22] else None
            aux = (u16le(b, 69), u16le(b, 71))
            e["aux_temp_c"] = [round(v / 10, 1) for v in aux if v is not None]
            st = u16le(b, 73)
            if st is not None:
                e["skin_temp_c"] = round(st / 100, 2)
            steps = u16le(b, 57)
            if steps is not None:
                e["steps"] = steps
            act = {0: "still", 1: "walk", 2: "run"}.get(b[63])
            if act:
                e["activity"] = act
            if len(b) > 81:
                e["motion_state"] = {0: "WAKE", 1: "STILL", 2: "SLEEP",
                                      3: "UP"}.get((b[81] >> 4) & 3)
    elif e.get("char", "").lower() == HR_MEAS_UUID:
        try:
            bpm, rr = parse_hr_measurement(b)
            e["hr"] = bpm
            if rr:
                e["rr_ms"] = [round(x, 1) for x in rr]
        except Exception:
            pass
    return e


# ---------------------------------------------------------------------------
# Historical capture orchestrator
# ---------------------------------------------------------------------------

class Capture:
    def __init__(self, bridge):
        self.bridge = bridge
        self.lock = threading.Lock()
        self.status = "idle"
        self.frames = []
        self.range_info = None
        self.started_at = None
        self.thread = None

    def snapshot(self):
        with self.lock:
            return {"status": self.status, "frames": len(self.frames),
                    "range": self.range_info,
                    "started_at": self.started_at}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return {"error": "capture already running"}
            self.frames = []
            self.range_info = None
            self.status = "running"
            self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
            os.makedirs(os.path.join(REPO_ROOT, "captures"), exist_ok=True)
            self.capture_path = os.path.join(
                REPO_ROOT, "captures",
                f"capture-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
            self._fh = open(self.capture_path, "a", encoding="utf-8")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return {"ok": True, "status": "running",
                "persisting": self.capture_path}

    def _persist(self, rec):
        """Write a record to disk BEFORE any acknowledgement (NOOP storage-
        ownership contract: an ack may release device history)."""
        self._fh.write(json.dumps(rec) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def _set(self, status):
        with self.lock:
            self.status = status

    def _run(self):
        br = self.bridge
        try:
            baseline = br.get("/state")["log_last_id"]
            br.send_command("get_data_range")
            time.sleep(1.5)
            br.send_command("send_historical_data")

            last_id = baseline
            idle_since = time.time()
            while time.time() - idle_since < 30:
                log = br.get(f"/log?since={last_id}")
                last_id = log["last_log_id"]
                got = False
                for e in log["entries"]:
                    if e.get("kind") != "notify" or not e.get("hex"):
                        continue
                    b = bytes.fromhex(e["hex"])
                    if len(b) < 9 or b[0] != 0xAA:
                        continue
                    ptype = b[8]
                    if ptype == 36 and b[10] == 34 and len(b) >= 14:
                        with self.lock:
                            self.range_info = b[13:].hex()
                    elif ptype == 49:
                        kind = b[10] if len(b) > 10 else None
                        got = True
                        if kind == 2 and len(b) >= 29:
                            # HISTORY_END: echo the 8-byte token verbatim
                            # (frame[21:29], per the Android ACK builder), but
                            # ONLY after the chunk is persisted to disk — the
                            # ack advances the band's trim boundary and may
                            # release stored history (NOOP storage-ownership).
                            token = b[21:29]
                            first_word = struct.unpack_from("<I", token, 0)[0]
                            if first_word == 0xFFFFFFFF:
                                # special completion token — no ordinary ack
                                self._set("done (completion token)")
                                self._fh.close()
                                return
                            self._persist({"ts": e.get("ts"), "event": "history_end",
                                           "token": token.hex()})
                            time.sleep(0.3)
                            br.send_command("historical_data_result",
                                            "01" + token.hex())
                        elif kind == 3:
                            self._set("done")
                            self._fh.close()
                            return
                    elif ptype == 47:
                        got = True
                        rec = {"ts": e.get("ts"), "hex": e["hex"],
                               "packet_k": b[10] if len(b) > 10 else None,
                               "page": u32le(b, 12),
                               "unix": u32le(b, 16),
                               "body_hex": b[22:].hex() if len(b) > 22 else ""}
                        self._persist(rec)
                        with self.lock:
                            self.frames.append(rec)
                if got:
                    idle_since = time.time()
                time.sleep(0.8)
            self._set("timeout (no complete marker) — kept what arrived; "
                      f"persisted to {getattr(self, 'capture_path', '?')}")
            try:
                self._fh.close()
            except Exception:
                pass
        except Exception as ex:
            self._set(f"error: {ex}")
            try:
                self._fh.close()
            except Exception:
                pass

    def download_payload(self):
        with self.lock:
            path = getattr(self, "capture_path", None)
        if path and os.path.exists(path):
            with open(path, "rb") as fh:
                return fh.read()   # raw JSONL as persisted (safe to ack against)
        return json.dumps({"captured_at": self.started_at,
                           "range_hex": self.range_info,
                           "frame_count": len(self.frames),
                           "frames": self.frames}, indent=1).encode()


CAPTURE = None


def switch_backend(url):
    """Runtime mode switch — no dashboard restart needed.
    url 'none'/'direct' → direct BLE; otherwise probe the bridge URL and
    adopt it (any bridge: phone app, bridge_server, pi_gateway)."""
    global BRIDGE, CAPTURE, MODE
    if str(url).lower() in ("none", "direct", ""):
        if not (isinstance(BRIDGE, DirectBridge) and BRIDGE.thread
                and BRIDGE.thread.is_alive()):
            db = DirectBridge()
            db.start()
            BRIDGE = db
        MODE = "direct"
        CAPTURE = None
        return {"ok": True, "mode": "direct"}
    cand = Bridge(url)
    cand.probe()   # raises if unreachable
    if isinstance(BRIDGE, DirectBridge):
        BRIDGE.stop()   # free the band for the bridge to claim
    BRIDGE, MODE = cand, "bridge"
    CAPTURE = Capture(BRIDGE)   # capture state resets on switch
    return {"ok": True, "mode": "bridge", "url": cand.url}


# ---------------------------------------------------------------------------
# HTTP server (dashboard itself)
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

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            return {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                self._page()
            elif u.path == "/api/catalog":
                self._json({name: list(entry) for name, entry in COMMAND_CATALOG.items()})
            elif u.path == "/api/state":
                s = BRIDGE.get("/state")
                # normalize across bridge shapes (web bridge reports
                # 'phone' instead of 'connected')
                if "connected" not in s and "phone" in s:
                    s["connected"] = s.get("phone") == "connected"
                    s["state"] = s.get("phone")
                s["capture"] = CAPTURE.snapshot() if CAPTURE else {
                    "status": "locked (needs a bridge)", "frames": 0,
                    "range": None, "started_at": None}
                s["mode"] = MODE
                s["bridge_url"] = getattr(BRIDGE, "url", "direct-ble (local radio)")
                s.setdefault("info", {})
                self._json(s)
            elif u.path == "/api/backend":
                self._json({"mode": MODE,
                            "url": getattr(BRIDGE, "url", None)})
            elif u.path == "/api/log":
                since = q.get("since", ["0"])[0]
                log = BRIDGE.get(f"/log?since={since}")
                log["entries"] = [enrich_entry(e) for e in log.get("entries", [])]
                self._json(log)
            elif u.path == "/api/capture/status":
                self._json(CAPTURE.snapshot())
            elif u.path == "/api/capture/download":
                data = CAPTURE.download_payload()
                data = data.encode() if isinstance(data, str) else data
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Disposition",
                                 'attachment; filename="whoop_capture.jsonl"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/help":
                self._json({"endpoints": ["/", "/api/catalog", "/api/state",
                                          "/api/log?since=N", "/api/send",
                                          "/api/frame", "/api/haptics", "/api/stop",
                                          "/api/capture/start", "/api/capture/status",
                                          "/api/capture/download"]})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as ex:
            self._json({"error": f"bridge unreachable or error: {ex}"}, 200)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        try:
            if u.path == "/api/send":
                self._json(BRIDGE.send_command(body.get("name"),
                                                body.get("payload")))
            elif u.path == "/api/frame":
                frame = body.get("frame", "")
                if not frame:
                    self._json({"error": "frame hex required"})
                else:
                    self._json(BRIDGE.write_raw(frame))
            elif u.path == "/api/haptics":
                self._json(self._haptics(body))
            elif u.path == "/api/stop":
                out = []
                for number, payload in STOP_SEQUENCE:
                    name = next((n for n, e in COMMAND_CATALOG.items()
                                 if e[0] == number), f"cmd_{number}")
                    out.append(BRIDGE.send_command(name, payload))
                self._json({"ok": True, "sent": out})
            elif u.path == "/api/capture/start":
                if MODE != "bridge" or not CAPTURE:
                    self._json({"error": "capture needs a bonded bridge (direct mode is public-GATT only)"})
                else:
                    self._json(CAPTURE.start())
            elif u.path == "/api/backend":
                self._json(switch_backend(body.get("url", "")))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as ex:
            self._json({"error": str(ex)}, 200)

    def _haptics(self, body):
        """Two modes:
        - pattern (default): ad-hoc buzz via cmd 19 — 12-byte body, no alarm
          slot consumed: [01, 8 effects, u16 loop, u8 overall]
        - alarm: set_alarm_time (21-byte record incl. crescendo) + optional run
        Overall-repeat counts pulses AFTER the first (N-1 semantics, NOOP)."""
        import calendar
        waveform = body.get("waveform") or [47, 152, 0, 0, 0, 0, 0, 0]
        if len(waveform) != 8 or not all(isinstance(x, int) and 0 <= x <= 255 for x in waveform):
            return {"error": "waveform must be 8 bytes (0-255; effects <= 251)"}
        loop_control = int(body.get("loop", 0))
        overall = int(body.get("overall", 7))

        if body.get("mode", "pattern") == "pattern":
            payload = bytes([1]) + bytes(waveform)
            payload += struct.pack("<H", loop_control) + bytes([overall])
            seq = BRIDGE.next_seq()
            frame = build_v5_frame(seq, 19, payload)
            r = BRIDGE.write_raw(frame.hex())
            return {"set": r, "frame": frame.hex(), "seq": seq,
                    "note": f"ad-hoc pattern: ~{overall + 1} pulses"}

        alarm_id = int(body.get("alarm_id", 1))
        if alarm_id < 1 or alarm_id > 6:
            return {"error": "alarm IDs are 1-6"}
        minutes = int(body.get("minutes", 5))
        schedule = int(time.time()) + minutes * 60
        duration = int(body.get("duration", 30))
        crescendo = int(body.get("crescendo", 0))

        payload = bytes([4, alarm_id])
        payload += struct.pack("<I", schedule)
        payload += struct.pack("<H", 0)
        payload += bytes(waveform)
        payload += struct.pack("<H", loop_control)
        payload += bytes([overall, duration, crescendo])

        seq = BRIDGE.next_seq()
        frame = build_v5_frame(seq, 66, payload)
        r = BRIDGE.write_raw(frame.hex())
        result = {"set": r, "frame": frame.hex(), "seq": seq,
                  "schedule_unix": schedule,
                  "note": f"alarm {alarm_id} set for +{minutes}min (crescendo={crescendo})"}
        if body.get("run_now"):
            time.sleep(0.5)
            run = BRIDGE.send_command("run_alarm", f"02{alarm_id:02x}")
            result["run"] = run
        return result

    def _page(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# The dashboard page
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>WHOOP dashboard</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; background: #121417; color: #d8dcd9;
         margin: 0; display: flex; flex-direction: column; height: 100vh; }
  header { display: flex; align-items: center; gap: 1rem; padding: .6rem 1rem;
           background: #1b1f24; border-bottom: 1px solid #2a2f36; }
  header h1 { font-size: 1rem; margin: 0; color: #8be9a8; }
  #status { font-size: .85rem; color: #9aa3ad; }
  .dot { display: inline-block; width: .6rem; height: .6rem; border-radius: 50%;
         margin-right: .3rem; background: #c33; }
  .dot.on { background: #3c3; }
  #stopbtn { margin-left: auto; background: #a02; color: #fff; border: none;
             padding: .45rem 1rem; border-radius: .4rem; font-weight: 700; cursor: pointer; }
  main { display: flex; flex: 1; min-height: 0; }
  #stream { flex: 1.4; display: flex; flex-direction: column; min-width: 0;
            border-right: 1px solid #2a2f36; }
  #streambar { padding: .4rem .8rem; font-size: .8rem; color: #9aa3ad;
               display: flex; gap: 1rem; align-items: center; }
  #rows { flex: 1; overflow-y: auto; font-family: ui-monospace, monospace;
          font-size: .72rem; padding: .3rem .8rem; }
  .row { padding: .12rem 0; border-bottom: 1px solid #1c2026; white-space: nowrap;
         overflow: hidden; text-overflow: ellipsis; }
  .k-notify { color: #9fd6ff; } .k-queued { color: #777; }
  .k-write_result { color: #6c6; } .k-write_attempt { color: #997; }
  .ev { color: #ffd479; } .rsp { color: #8be9a8; } .hrv { color: #ff9fd6; }
  .tag { display: inline-block; padding: 0 .3rem; border-radius: .2rem;
         background: #262b31; margin-right: .35rem; color: #cdd4da; }
  aside { flex: 1; overflow-y: auto; padding: .8rem; min-width: 340px; }
  h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .06em;
       color: #7d8791; margin: 1rem 0 .4rem; }
  h2:first-child { margin-top: 0; }
  .grp { display: flex; flex-wrap: wrap; gap: .35rem; margin-bottom: .6rem; }
  button.cmd { font-size: .75rem; padding: .35rem .6rem; border: none;
               border-radius: .35rem; cursor: pointer; background: #2c3138; color: #d8dcd9; }
  button.cmd.read { background: #1d3a29; } button.cmd.stream { background: #3a341d; }
  button.cmd.sync { background: #1d2c3a; } button.cmd.alarm { background: #3a1d24; }
  button.cmd.clock { background: #2a2438; }
  .hint { font-size: .7rem; color: #7d8791; margin: .2rem 0 .6rem; }
  input, select { background: #1b1f24; border: 1px solid #2a2f36; color: #d8dcd9;
                  border-radius: .3rem; padding: .25rem .4rem; font-size: .75rem; }
  body.direct button.cmd, body.direct #stopbtn, body.direct .go,
  body.direct .buzz, body.direct .capbtn { opacity: .3; pointer-events: none; }
  body.direct #modelock { display: block; }
  #modelock { display: none; background: #3a341d; color: #ffe08a; font-size: .72rem;
              padding: .45rem .6rem; border-radius: .35rem; margin-bottom: .6rem; }
  input[type=number] { width: 4rem; }
  .slots input { width: 2.6rem; text-align: center; margin-right: .2rem; }
  #miniconsole { font-family: ui-monospace, monospace; font-size: .7rem;
                 color: #9aa3ad; background: #171a1e; border-radius: .4rem;
                 padding: .4rem; max-height: 8rem; overflow-y: auto; margin-top: .5rem; }
  .big { padding: .55rem 1rem; border: none; border-radius: .4rem; cursor: pointer;
         font-size: .8rem; font-weight: 600; }
  .go { background: #2f6e46; color: #fff; } .buzz { background: #7a4a9c; color: #fff; }
  .capbtn { background: #2c5270; color: #fff; }
  label { font-size: .72rem; color: #9aa3ad; }
</style></head><body>
<header>
  <h1>WHOOP dashboard</h1>
  <span class="dot" id="dot"></span><span id="status">connecting…</span>
  <button id="stopbtn" onclick="stopAll()">■ STOP ALL STREAMS</button>
</header>
<main>
  <section id="stream">
    <div id="streambar">
      <label><input type="checkbox" id="autoscroll" checked> autoscroll</label>
      <label><input type="checkbox" id="showhr" checked> show 1Hz packets</label>
      <label><input type="checkbox" id="plumbing"> show plumbing</label>
      <span id="lastid" style="margin-left:auto"></span>
    </div>
    <div id="rows"></div>
  </section>
  <aside>
    <h2>Device</h2>
    <div id="device" class="hint">waiting for a connection…</div>

    <h2>Bridge</h2>
    <div id="modeline" style="font-size:.75rem; color:#8be9a8; margin-bottom:.35rem">mode: ...</div>
    <div style="display:flex; gap:.35rem; margin-bottom:.35rem">
      <input id="bridgeurl" value="http://localhost:8443" style="flex:1">
      <button class="cmd read" onclick="connectBridge()">Connect</button>
      <button class="cmd" onclick="directMode()">Direct BLE</button>
    </div>
    <div id="unlock" class="hint"></div>

    <h2>Command console</h2>
    <div id="modelock">DIRECT MODE - this machine has no bond with the band, so the
      WHOOP custom service (the whole catalog) returns <i>Insufficient Authentication</i>.
      Public reads (device info, battery) stream automatically once connected.
      Use the Bridge panel above to unlock.</div>
    <div class="hint">green = safe reads · amber = stream toggles · blue = historical · red = alarms/haptics. State-changing commands ask first.</div>
    <div id="console"></div>
    <div id="miniconsole"></div>

    <h2>Haptics lab</h2>
    <div class="hint">waveform = 8 effect slots ([47,152] is the WHOOP default pair; effects <= 251). Overall = pulses AFTER the first (N-1). "BUZZ" uses ad-hoc cmd 19 — no alarm slot. "Set alarm" writes the 21-byte record (slots 1-6, crescendo optional) and can run it now.</div>
    <div class="slots" id="slots"></div>
    <label>loop <input type="number" id="hloop" value="0"></label>
    <label>overall <input type="number" id="hoverall" value="2"></label>
    <label>alarm id <input type="number" id="hid" value="1" min="1" max="6"></label>
    <label>+min <input type="number" id="hmin" value="5"></label>
    <label>dur(s) <input type="number" id="hdur" value="30"></label>
    <label>crescendo <select id="hcres"><option value="0">0 (pattern)</option><option value="1">1 (staged)</option></select></label>
    <div style="margin-top:.4rem; display:flex; gap:.5rem">
      <button class="big buzz" onclick="haptics('pattern')">BUZZ NOW</button>
      <button class="big go" onclick="haptics('alarm', true)">Set + alarm now</button>
      <button class="cmd" onclick="api('/api/send',{method:'POST',body:JSON.stringify({name:'stop_haptics'})}).then(r=>mini('stop → '+JSON.stringify(r)))">Stop</button>
    </div>

    <h2>Historical grab</h2>
    <div class="hint">Walks the page-pointer dance: get_data_range → send_historical_data → collects type-47 pages, acks bursts, stops on HistoryComplete (or 30 s idle).</div>
    <div style="display:flex; gap:.5rem; align-items:center">
      <button class="big capbtn" onclick="capStart()">Start capture</button>
      <a id="capdl" href="/api/capture/download" style="display:none">
        <button class="big capbtn">Download JSON</button></a>
    </div>
    <div class="hint" id="capstatus">idle</div>
  </aside>
</main>
<script>
let catalog = {}, lastId = 0, capFrameCount = -1;

async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}
function mini(msg) {
  const el = document.getElementById('miniconsole');
  el.textContent = new Date().toLocaleTimeString() + '  ' + msg + '\n' + el.textContent;
}
const PLUMBING = new Set(['scan', 'gatt', 'cccd', 'http', 'queued', 'write_attempt']);
function rowLine(e) {
  const cls = 'k-' + (e.kind || 'x');
  let extra = '';
  if (e.kind === 'info') return `<div class="row ${cls}" style="color:#8be9a8">${e.ts || ''} <span class="tag">info</span>${e.desc || ''}</div>`;
  if (e.event_name) extra = `<span class="tag ev">${e.event_name}</span>`;
  else if (e.result) extra = `<span class="tag rsp">${e.result}${e.haptics ? ' / ' + e.haptics : ''}</span>`;
  else if (e.meta) extra = `<span class="tag">${e.meta}${e.end_token ? ' ' + e.end_token : ''}</span>`;
  else if (e.hr) extra = `<span class="tag hrv">HR ${e.hr}${e.rr_ms ? ' · RR ' + e.rr_ms.join(',') : ''}${e.skin_temp_c ? ' · ' + e.skin_temp_c + '°C' : ''}${e.steps !== undefined ? ' · steps ' + e.steps : ''}${e.activity ? ' · ' + e.activity : ''}${e.motion_state ? ' · ' + e.motion_state : ''}</span>`;
  else if (e.type_name) extra = `<span class="tag">${e.type_name}${e.domain ? ' ' + e.domain : ''}</span>`;
  return `<div class="row ${cls}">${e.ts || ''} <span class="tag">${e.kind}</span>${extra}${e.hex || ''}</div>`;
}
function pollLog() {
  api('/api/log?since=' + lastId).then(j => {
    if (j.error) { document.getElementById('status').textContent = j.error; return; }
    lastId = j.last_log_id;
    document.getElementById('lastid').textContent = 'log #' + lastId;
    const rows = document.getElementById('rows');
    const showhr = document.getElementById('showhr').checked;
    const showPlumbing = document.getElementById('plumbing').checked;
    for (const e of j.entries || []) {
      if (!showhr && e.type === 40) continue;
      if (!showPlumbing && PLUMBING.has(e.kind)) continue;
      rows.insertAdjacentHTML('afterbegin', rowLine(e));
    }
    while (rows.children.length > 300) rows.removeChild(rows.lastChild);
    if (document.getElementById('autoscroll').checked && j.entries && j.entries.length)
      rows.scrollTop = 0;
  }).catch(() => {});
}
function unlockSteps(mode) {
  const el = document.getElementById('unlock');
  if (mode !== 'direct') {
    el.innerHTML = 'Connected to a bridge - full catalog unlocked.';
    return;
  }
  el.innerHTML =
    '<b>Direct mode: public GATT only.</b> To unlock the custom service, a bonded ' +
    'bridge must hold the band. Pick ONE:' +
    '<br><b>1. Phone app (recommended - your phone already has the bond):</b> ' +
    'plug phone in (USB debugging) &gt; <code>adb forward tcp:8443 tcp:8443</code> &gt; ' +
    'force-stop the WHOOP app &gt; open the WHOOP Bridge app &gt; tap CONNECT &gt; ' +
    'press Connect above (localhost:8443).' +
    '<br><b>2. Phone browser:</b> run <code>python bridges/bridge_server.py</code> on ' +
    'the PC, open the URL it prints in phone Chrome, accept the cert, tap CONNECT, ' +
    'then Connect above.' +
    '<br><b>3. Linux box / Pi:</b> run <code>python3 bridges/pi_gateway.py pair</code> ' +
    'once, then <code>serve</code>, then enter <code>http://PI-IP:8443</code> above ' +
    'and Connect. Details: README, The bridges.';
}
async function connectBridge() {
  const url = document.getElementById('bridgeurl').value.trim() || 'http://localhost:8443';
  mini('switching bridge -> ' + JSON.stringify(
    await api('/api/backend', {method: 'POST', body: JSON.stringify({url})})));
}
async function directMode() {
  mini('-> ' + JSON.stringify(
    await api('/api/backend', {method: 'POST', body: JSON.stringify({url: 'none'})})));
}
function pollState() {
  api('/api/state').then(s => {
    const ok = s.connected === true;
    document.getElementById('dot').className = 'dot' + (ok ? ' on' : '');
    document.body.classList.toggle('direct', s.mode === 'direct');
    document.getElementById('modeline').textContent =
      'mode: ' + (s.mode === 'direct' ? 'DIRECT BLE (public GATT only)'
                                      : 'bridge -> ' + (s.bridge_url || ''));
    unlockSteps(s.mode);
    const dev = s.info || {};
    let dh = '';
    if (s.band) dh += `<span class="tag">name</span>${s.band}<br>`;
    for (const [k, v] of Object.entries(dev)) dh += `<span class="tag">${k}</span>${v}<br>`;
    if (s.mode === 'bridge' && !Object.keys(dev).length)
      dh += '<i>public reads happen in direct mode; bridge mode carries the band name only</i>';
    document.getElementById('device').innerHTML = dh || 'waiting for a connection…';
    document.getElementById('status').textContent =
      (s.error ? s.error :
        (ok ? `${s.band || 'band'} · ${s.state || 'ready'} · queued ${s.queued}` :
              (s.mode === 'direct' ? 'direct BLE — scanning/waiting' : 'bridge up, band not connected')));
    const cap = s.capture || {};
    document.getElementById('capstatus').textContent =
      `${cap.status || 'idle'} · ${cap.frames || 0} frames` +
      (cap.range ? ` · range ${cap.range}` : '');
    const dl = document.getElementById('capdl');
    dl.style.display = (cap.frames > 0) ? '' : 'none';
  }).catch(() => { document.getElementById('status').textContent = 'dashboard server down?'; });
}
const DEADLY = /^(forget_bonds|force_trim|set_read_pointer|reboot_strap|power_cycle_strap)$/;
function category(name) {
  if (DEADLY.test(name)) return ['DANGEROUS - service', 'alarm'];
  if (/alarm|haptic/.test(name)) return ['Alarms & haptics', 'alarm'];
  if (/historical|high_freq|data_range/.test(name)) return ['Historical sync', 'sync'];
  if (/clock|advertising|wear_detection|led_accessibility|body_location/.test(name)) return ['Device & identity', 'clock'];
  if (/config|_ff$|_ff_|afe|gyro|signal_processing/.test(name)) return ['Configuration', 'clock'];
  if (/ecg|labrador|wrist/.test(name)) return ['ECG (MG only)', 'sync'];
  if (/battery/.test(name)) return ['Battery & power', 'read'];
  if (/raw_data|ble_uart/.test(name)) return ['Raw data', 'stream'];
  if (/^get_|^link_valid/.test(name)) return ['Reads', 'read'];
  return ['Stream toggles', 'stream'];
}
async function sendCmd(name, payload) {
  const entry = catalog[name];
  let p = (payload === undefined || payload === null) ? entry[1] : payload;
  if (p === null) { p = prompt('payload hex required for ' + name); if (!p) return; }
  const safe = /^get_/.test(name) || name === 'link_valid';
  if (!safe && !confirm(`Send ${name} (payload ${p})?\n\n${entry[2]}`)) return;
  if (DEADLY.test(name) &&
      prompt(`"${name}" is flagged DANGEROUS.\nType the command name exactly to send:`) !== name) {
    mini(`aborted ${name} - typed confirmation failed`); return;
  }
  const r = await api('/api/send', {method: 'POST', body: JSON.stringify({name, payload: p})});
  mini(`send ${name} → ${JSON.stringify(r)}`);
}
function buildConsole() {
  api('/api/catalog').then(cat => {
    catalog = cat;
    const groups = {};
    for (const name of Object.keys(cat).sort()) {
      const [label, cls] = category(name);
      (groups[label] = groups[label] || []).push({name, cls});
    }
    const el = document.getElementById('console');
    el.innerHTML = '';
    for (const [label, cmds] of Object.entries(groups)) {
      const h = document.createElement('h2'); h.textContent = label; el.appendChild(h);
      const g = document.createElement('div'); g.className = 'grp';
      for (const c of cmds) {
        const b = document.createElement('button');
        b.className = 'cmd ' + c.cls; b.textContent = name2label(c.name);
        b.title = `#${cat[c.name][0]} · payload ${cat[c.name][1] ?? '<required>'} · ${cat[c.name][2]}`;
        b.onclick = () => sendCmd(c.name);
        g.appendChild(b);
      }
      el.appendChild(g);
    }
  });
}
function name2label(n) { return n.replace(/_/g, ' '); }
async function stopAll() {
  if (!confirm('Send the full stopPhysiologyCapture sequence (all streams OFF)?')) return;
  mini('STOP sequence → ' + JSON.stringify(await api('/api/stop', {method:'POST'})));
}
function haptics(mode, runNow) {
  const wf = [...document.querySelectorAll('#slots input')].map(i => parseInt(i.value || '0'));
  const body = { waveform: wf, loop: +hloop.value, overall: +hoverall.value,
                 duration: +hdur.value, crescendo: +hcres.value,
                 alarm_id: +hid.value, minutes: +hmin.value,
                 mode: mode, run_now: !!runNow };
  const label = mode === 'pattern' ? 'BUZZ the band NOW (ad-hoc pattern)'
                                   : `Set alarm ${hid.value} and run NOW`;
  if (!confirm(label + `?\nwaveform [${wf}] overall ${hoverall.value}`)) return;
  api('/api/haptics', {method: 'POST', body: JSON.stringify(body)})
    .then(r => mini('haptics → ' + JSON.stringify(r)));
}
async function capStart() {
  if (!confirm('Start historical capture? The band will stream stored pages.')) return;
  mini('capture → ' + JSON.stringify(await api('/api/capture/start', {method:'POST'})));
}
// init
(function () {
  const slots = document.getElementById('slots');
  [47,152,0,0,0,0,0,0].forEach(v => {
    const i = document.createElement('input');
    i.type = 'number'; i.value = v; i.min = 0; i.max = 255;
    slots.appendChild(i);
  });
  buildConsole();
  pollState(); setInterval(pollState, 2000);
  pollLog(); setInterval(pollLog, 1000);
})();
</script></body></html>
"""


# ---------------------------------------------------------------------------

def main(argv=None):
    global BRIDGE, CAPTURE, MODE
    if isinstance(argv, argparse.Namespace):
        args = argv          # called via whoop_ble.py dashboard subcommand
    else:
        parser = argparse.ArgumentParser(
            description="WHOOP bridge dashboard — browser frontend to any bridge.")
        parser.add_argument("--bridge", default="http://localhost:8443",
                            help="bridge base URL ('none' forces direct BLE mode)")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8000)
        args = parser.parse_args(argv)

    if args.bridge.lower() in ("none", "direct"):
        MODE = "direct"
        BRIDGE = DirectBridge()
        BRIDGE.start()
    else:
        candidate = Bridge(args.bridge)
        try:
            candidate.probe()
            MODE = "bridge"
            BRIDGE = candidate
        except Exception:
            MODE = "direct"
            BRIDGE = DirectBridge()
            BRIDGE.start()
    CAPTURE = Capture(BRIDGE) if MODE == "bridge" else None
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    mode_note = ("bridge mode - full catalog" if MODE == "bridge"
                 else "DIRECT mode - no bridge found: public GATT only, "
                      "catalog locked (no bond on this machine). "
                      "Use the Bridge panel in the UI to connect one.")
    print(f"WHOOP dashboard: http://{args.host}:{args.port}/  [{mode_note}]")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
