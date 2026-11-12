#!/usr/bin/env python3
"""
whoop_ble.py — single-file CLI for talking to a WHOOP 5.0 band over Bluetooth LE.

Protocol knowledge reverse-engineered by the Goose project (github.com/b-nnett/goose),
which itself drew on OpenWHOOP and the WHOOP Android APK. This repo is not
affiliated with WHOOP. Use at your own risk.

Requirements:
    pip install bleak        (only needed for BLE commands; `frame`/`commands` work offline)

On Windows from WSL, run with the Windows Python:   python.exe whoop_ble.py ...
"""

import argparse
import asyncio
import struct
import sys
import zlib

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WHOOP_V5_SERVICE = "fd4b0001-cce1-4033-93ce-002d5875f58a"
WHOOP_GEN4_SERVICE = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
WHOOP_SERVICES = {WHOOP_V5_SERVICE, WHOOP_GEN4_SERVICE}

CMD_CHAR_UUIDS = {  # write commands here
    "fd4b0002-cce1-4033-93ce-002d5875f58a",
    "61080002-8d6d-82b8-614a-1c8cb0f8dcc6",
}
NOTIFY_CHAR_UUIDS = {  # data comes out here
    "fd4b0003-cce1-4033-93ce-002d5875f58a",
    "fd4b0004-cce1-4033-93ce-002d5875f58a",
    "fd4b0005-cce1-4033-93ce-002d5875f58a",
    "fd4b0007-cce1-4033-93ce-002d5875f58a",
    "61080003-8d6d-82b8-614a-1c8cb0f8dcc6",
    "61080004-8d6d-82b8-614a-1c8cb0f8dcc6",
    "61080005-8d6d-82b8-614a-1c8cb0f8dcc6",
    "61080007-8d6d-82b8-614a-1c8cb0f8dcc6",
}

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEAS_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
DEVICE_INFO_UUIDS = {  # all readable without pairing
    "00002a24-0000-1000-8000-00805f9b34fb": "model number",
    "00002a25-0000-1000-8000-00805f9b34fb": "serial number",
    "00002a26-0000-1000-8000-00805f9b34fb": "firmware revision",
    "00002a27-0000-1000-8000-00805f9b34fb": "hardware revision",
    "00002a28-0000-1000-8000-00805f9b34fb": "software revision",
    "00002a29-0000-1000-8000-00805f9b34fb": "manufacturer name",
}

# Known-good client hello captured from the official pairing flow (seq=1, cmd=145).
HELLO_FRAME = bytes.fromhex("aa0108000001e67123019101363e5c8d")

# WHOOP V5 packet types (first payload byte of a frame).
PACKET_TYPE_NAMES = {
    35: "command", 36: "command_response", 37: "puffin_command",
    38: "puffin_command_response", 40: "realtime_data",
    43: "realtime_raw_data", 47: "historical_data", 48: "event",
    49: "metadata", 50: "console_logs", 51: "realtime_imu_stream",
    52: "historical_imu_stream", 53: "relative_puffin_events",
    54: "puffin_events_from_strap", 55: "battery_pack_console_logs",
    56: "puffin_metadata",
}

# Reverse-engineered command catalog: name -> (number, default payload hex or None
# if a payload is mandatory, one-line description). Payloads are hex WITHOUT the
# leading sequence/command bytes.
COMMAND_CATALOG = {
    "get_hello":                  (145, "01", "pairing/handshake hello (known-good frame)"),
    "toggle_realtime_hr":         (3,   "01", "enable live HR stream; 00 disables"),
    "send_r10_r11_realtime":      (63,  "01", "enable R10/R11 realtime stream; 00 disables"),
    "get_clock":                  (11,  "",    "read the strap's clock"),
    "set_clock":                  (10,  None,  "set strap clock; payload = u32le seconds + u32le subseconds"),
    "get_data_range":             (34,  "",    "read historical storage page pointers"),
    "send_historical_data":       (22,  "",    "request bulk historical data stream"),
    "historical_data_result":     (23,  "0100000000000000", "ack historical packet receipt"),
    "enter_high_freq_sync":      (96,  None,  "payload = 02 + u16le interval + u16le duration (seconds)"),
    "exit_high_freq_sync":        (97,  "",    "leave high-frequency sync mode"),
    "set_alarm_time":             (66,  None,  "payload = 04 id u32le sec u16le subsec + 8 waveform + u16le loop + u8 overall + u8 duration"),
    "get_alarm_time":             (67,  "04",  "payload = 04 + alarm id"),
    "run_alarm":                  (68,  "02",  "payload = 02 + alarm id"),
    "disable_alarm":              (69,  "0200ff", "disable all alarms"),
    "get_body_location_and_status": (84, "",   "body placement / strap status"),
    "get_research_packet":        (132, "",    "generic research packet read"),
    "get_extended_battery_info":  (98,  "",    "battery state beyond standard GATT"),
    "get_battery_pack_info":      (151, "01",  "battery pack info, revision 1"),
    "get_led_drive":              (40,  "",    "optical LED drive configuration"),
    "get_tia_gain":               (42,  "",     "optical transimpedance amp gain"),
    "get_bias_offset":            (44,  "",     "optical bias offset"),
    "toggle_imu_mode":            (106, "0101", "enable IMU/accelerometer stream; 0100 disables"),
    "toggle_imu_mode_historical": (105, None,  "historical IMU toggle (explicit payload required)"),
    "enable_optical_data":        (107, "0101", "enable optical/PPG data; 0100 disables"),
    "toggle_optical_mode":        (108, "0101", "enable optical sensor mode; 0100 disables"),
    "toggle_persistent_r20":      (153, "0101", "persistent sensor mode R20; 0100 disables"),
    "toggle_persistent_r21":      (154, "0101", "persistent sensor mode R21; 0100 disables"),
    "get_device_config_value":    (121, None,  "payload = 01 + 32-byte config key"),
    "get_feature_flag_value":     (128, None,  "payload = 01 + 32-byte feature-flag key"),
}

# ---------------------------------------------------------------------------
# Protocol: frame building (verified against Goose's known-good hello frame)
# ---------------------------------------------------------------------------

def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_v5_frame(seq: int, command: int, data) -> bytes:
    """Build a WHOOP V5 command frame.

    Layout: [0xAA 0x01 u16le_len 0x00 0x01 CRC16u16le] + payload + CRC32u32le
    where payload = [packet_type=35, seq, command, data..., zero-pad to 4]
    and u16le_len = len(payload) + 4.
    """
    payload = bytes([35, seq & 0xFF, command & 0xFF]) + bytes(data)
    payload += b"\x00" * ((4 - len(payload) % 4) % 4)
    declared = len(payload) + 4
    frame = bytes([0xAA, 0x01, declared & 0xFF, (declared >> 8) & 0xFF, 0x00, 0x01])
    frame += crc16_modbus(frame).to_bytes(2, "little")
    frame += payload
    frame += (zlib.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "little")
    return frame


def describe_frame(data: bytes) -> str:
    """Best-effort human-readable peek at a WHOOP notification frame."""
    if len(data) < 8 or data[0] != 0xAA:
        return ""
    pkt_type = data[8] if len(data) > 8 else None
    name = PACKET_TYPE_NAMES.get(pkt_type, f"type_{pkt_type}")
    seq = data[9] if len(data) > 9 else None
    cmd = data[10] if len(data) > 10 else None
    return f"[{name} seq={seq} cmd/event={cmd} len={len(data)}]"


def parse_hr_measurement(data: bytes):
    """Parse standard Bluetooth Heart Rate Measurement (0x2A37). Returns (bpm, [rr_ms])."""
    flags = data[0]
    bpm, i = None, 1
    if flags & 0x01:
        bpm = struct.unpack_from("<H", data, i)[0]
        i += 2
    else:
        bpm = data[i]
        i += 1
    rr = []
    if flags & 0x10:  # RR intervals present, units of 1/1024 s
        while i + 2 <= len(data):
            rr.append(struct.unpack_from("<H", data, i)[0] / 1024.0 * 1000.0)
            i += 2
    return bpm, rr

# ---------------------------------------------------------------------------
# BLE helpers
# ---------------------------------------------------------------------------

def looks_like_whoop(name, adv_services) -> bool:
    if name and "whoop" in name.lower():
        return True
    return any(str(s).lower() in WHOOP_SERVICES for s in (adv_services or []))


async def find_whoop(seconds: float, address: str = None):
    """Scan for the WHOOP band. Returns (BLEDevice, advertisement data) or (None, None)."""
    from bleak import BleakScanner
    if address:
        # BleakScanner.from_address is not supported on Windows; scan and match.
        print(f"Looking for {address} ...")
        target = address.lower().replace("-", ":")
        devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
        for addr, (d, adv) in devices.items():
            if str(addr).lower() == target:
                return d, adv
        return None, None

    print(f"Scanning for {seconds:.0f}s looking for a WHOOP ...")
    devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
    for addr, (d, adv) in devices.items():
        name = d.name or (adv.local_name or "")
        if looks_like_whoop(name, adv.service_uuids or []):
            return d, adv
    return None, None


async def connect_to_whoop(seconds: float = 20.0, address: str = None):
    """Scan for and connect to the band. Returns an connected BleakClient."""
    from bleak import BleakClient
    device, adv = await find_whoop(seconds, address)
    if not device:
        print("No WHOOP found. Make sure phone Bluetooth is OFF (band stops")
        print("advertising while connected to the phone) and the band is charged.")
        sys.exit(1)
    print(f"Found: {(device.name or '?')!r} @ {device.address} (rssi {adv.rssi})")
    print(f"Connecting to {device.address} ...")
    client = BleakClient(device)
    await client.connect()
    print(f"Connected! MTU: {client.mtu_size}")
    return client


def print_service_map(client):
    print("\nService map:")
    for service in client.services:
        label = "WHOOP V5 service" if str(service.uuid).lower() == WHOOP_V5_SERVICE else \
                "WHOOP gen4 service" if str(service.uuid).lower() == WHOOP_GEN4_SERVICE else \
                str(service.uuid)
        print(f"  [service] {label}")
        for char in service.characteristics:
            print(f"      {char.uuid}  props={sorted(char.properties)}")


def find_char(client, uuids, prop=None):
    for svc in client.services:
        for char in svc.characteristics:
            u = str(char.uuid).lower()
            if u in uuids:
                if prop is None or prop in char.properties:
                    return char
    return None


async def log_standard_info(client):
    """Read the no-auth standard characteristics: device info + battery."""
    print("\nDevice info (no pairing needed):")
    for svc in client.services:
        for char in svc.characteristics:
            u = str(char.uuid).lower()
            if u in DEVICE_INFO_UUIDS and "read" in char.properties:
                try:
                    val = await client.read_gatt_char(char)
                    print(f"      {DEVICE_INFO_UUIDS[u]:<20} = {val.decode(errors='replace').rstrip(chr(0))!r}")
                except Exception as e:
                    print(f"      {DEVICE_INFO_UUIDS[u]:<20} read failed: {e}")
            elif u == BATTERY_LEVEL_UUID and "read" in char.properties:
                try:
                    val = await client.read_gatt_char(char)
                    print(f"      {'battery level':<20} = {val[0]}%")
                except Exception as e:
                    print(f"      battery level read failed: {e}")


def make_whoop_notification_handler():
    def handler(char, data: bytes):
        peek = describe_frame(data)
        print(f"  <-- [{str(char.uuid)[:8]}] {data.hex()}  {peek}")
    return handler


def make_hr_handler():
    def handler(_char, data: bytes):
        bpm, rr = parse_hr_measurement(data)
        suffix = f"  RR(ms)={[round(x, 1) for x in rr]}" if rr else ""
        print(f"  HR: {bpm} bpm{suffix}")
    return handler

# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------

async def cmd_scan(args):
    """scan: find advertising WHOOP bands (phone Bluetooth must be off)."""
    from bleak import BleakScanner
    seen_whoop = set()
    seen_other = {}

    def handler(device, adv_data):
        name = device.name or adv_data.local_name or ""
        if looks_like_whoop(name, adv_data.service_uuids or []):
            if device.address not in seen_whoop:
                seen_whoop.add(device.address)
                print(f"  *** WHOOP ***  {name!r} @ {device.address} (rssi {adv_data.rssi})")
                svc = [s for s in (adv_data.service_uuids or [])
                       if str(s).lower() in WHOOP_SERVICES]
                print(f"                advertises: {svc if svc else 'name only'}")
        elif args.all:
            key = device.address
            if name and key not in seen_other:
                seen_other[key] = name
                print(f"  {name!r:<45} rssi={adv_data.rssi}")
            elif not name and adv_data.service_uuids and key not in seen_other:
                seen_other[key] = "(unnamed)"
                print(f"  [unnamed] rssi={adv_data.rssi:<4} services={set(adv_data.service_uuids)}")

    print(f"Scanning for {args.seconds:.0f}s ...")
    scanner = BleakScanner(detection_callback=handler)
    async with scanner:
        await asyncio.sleep(args.seconds)
    if not seen_whoop:
        print("No WHOOP found.")
        print("  * phone Bluetooth off? (band stops advertising while phone is connected)")
        print("  * band charged and off the charger?")


async def cmd_info(args):
    """info: connect, dump service map, read device info + battery."""
    async with await connect_to_whoop(args.scan, args.address) as client:
        print_service_map(client)
        await log_standard_info(client)


async def cmd_hr(args):
    """hr: connect, subscribe to standard heart rate, print live BPM."""
    async with await connect_to_whoop(args.scan, args.address) as client:
        hr_char = find_char(client, {HR_MEAS_UUID}, "notify")
        if not hr_char:
            print("No standard heart-rate characteristic found.")
            return
        print("Subscribed to heart rate. The band usually only streams after the")
        print("`send toggle_realtime_hr` command succeeds (needs pairing).")
        await client.start_notify(hr_char, make_hr_handler())
        await asyncio.sleep(args.seconds)


async def cmd_hello(args):
    """hello: full probe - subscribe to everything, send client hello, log frames."""
    async with await connect_to_whoop(args.scan, args.address) as client:
        print_service_map(client)
        await log_standard_info(client)

        hr_char = find_char(client, {HR_MEAS_UUID}, "notify")
        if hr_char:
            print("\nSubscribing to standard heart rate ...")
            await client.start_notify(hr_char, make_hr_handler())

        print("\nSubscribing to WHOOP notification characteristics ...")
        whoop_chars = [c for c in
                       [find_char(client, {u}) for u in NOTIFY_CHAR_UUIDS] if c]
        auth_failed = False
        for c in whoop_chars:
            try:
                await client.start_notify(c, make_whoop_notification_handler())
                print(f"      subscribed: {c.uuid}")
            except Exception as e:
                auth_failed = "Authentication" in str(e) or "auth" in str(e)
                print(f"      FAILED {c.uuid}: {e}")
        if auth_failed:
            print("      -> custom service needs a PAIRED/bonded link (see README)")

        cmd_char = find_char(client, CMD_CHAR_UUIDS, "write")
        if cmd_char:
            print(f"\nSending client hello to {cmd_char.uuid}:")
            print(f"  --> {HELLO_FRAME.hex()}")
            try:
                await client.write_gatt_char(cmd_char, HELLO_FRAME,
                                              response="write" in cmd_char.properties)
            except Exception as e:
                print(f"      write FAILED: {e}")
                print("      -> pair Windows with the band and retry (see README)")
        await asyncio.sleep(args.seconds)


async def cmd_send(args):
    """send NAME: send a catalog command to the band's command characteristic."""
    entry = COMMAND_CATALOG.get(args.name)
    if not entry:
        print(f"Unknown command {args.name!r}. Use `commands` to list them.")
        sys.exit(2)
    number, default_payload, _desc = entry
    payload_hex = args.payload if args.payload is not None else default_payload
    if payload_hex is None:
        print("This command requires an explicit --payload (hex).")
        sys.exit(2)
    data = bytes.fromhex(payload_hex)
    frame = build_v5_frame(args.seq, number, data)

    print(f"Command {args.name} (#{number}) seq={args.seq} payload={data.hex()}")
    print(f"Frame: {frame.hex()}")
    if args.dry_run:
        return

    async with await connect_to_whoop(args.scan, args.address) as client:
        whoop_chars = [c for c in
                       [find_char(client, {u}) for u in NOTIFY_CHAR_UUIDS] if c]
        for c in whoop_chars:
            try:
                await client.start_notify(c, make_whoop_notification_handler())
            except Exception as e:
                print(f"      notify subscribe failed on {c.uuid}: {e}")
        cmd_char = find_char(client, CMD_CHAR_UUIDS, "write")
        if not cmd_char:
            print("No writable WHOOP command characteristic found.")
            return
        print(f"Writing to {cmd_char.uuid} ...")
        try:
            await client.write_gatt_char(cmd_char, frame,
                                         response="write" in cmd_char.properties)
        except Exception as e:
            print(f"      write FAILED: {e}")
            print("      -> custom service needs pairing (see README)")
        await asyncio.sleep(args.seconds)


async def cmd_frame(args):
    """frame SEQ CMD [PAYLOAD_HEX]: build a frame and print hex (offline, no BLE)."""
    data = bytes.fromhex(args.payload) if args.payload else b""
    print(build_v5_frame(args.seq, args.cmd, data).hex())


async def cmd_commands(args):
    """commands: list the reverse-engineered command catalog."""
    print(f"{'name':<30} {'#':>4}  default payload  description")
    print("-" * 100)
    for name, (number, payload, desc) in sorted(COMMAND_CATALOG.items(), key=lambda x: x[1][0]):
        p = payload if payload is not None else "<required>"
        print(f"{name:<30} {number:>4}  {p:<16} {desc}")
    print("\nExample: send get_hello          -> known-good handshake frame")
    print("         send toggle_realtime_hr -> enables live HR streaming")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="WHOOP 5.0 BLE toolkit - single-file CLI. "
                    "Protocol from github.com/b-nnett/goose. Not affiliated with WHOOP.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_ble_options(p, listen_default=30):
        p.add_argument("--seconds", type=float, default=listen_default,
                       help="how long to listen after connecting (default %(default)s)")
        p.add_argument("--scan", type=float, default=20.0,
                       help="BLE scan duration (default %(default)s)")
        p.add_argument("--address", default=None,
                       help="skip discovery and connect to this MAC address")

    p = sub.add_parser("scan", help="find advertising WHOOP bands")
    p.add_argument("--seconds", type=float, default=30.0)
    p.add_argument("--all", action="store_true", help="also list non-WHOOP devices")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("info", help="connect: service map + device info + battery")
    add_ble_options(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("hr", help="connect and stream standard heart rate")
    add_ble_options(p)
    p.set_defaults(func=cmd_hr)

    p = sub.add_parser("hello", help="full probe: subscribe all, send client hello")
    add_ble_options(p)
    p.set_defaults(func=cmd_hello)

    p = sub.add_parser("send", help="send a catalog command to the band")
    add_ble_options(p)
    p.add_argument("name", help="command name (see `commands`)")
    p.add_argument("--payload", default=None, help="hex payload (overrides catalog default)")
    p.add_argument("--seq", type=int, default=1, help="sequence byte (default 1)")
    p.add_argument("--dry-run", action="store_true", help="build and print the frame, don't connect")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("frame", help="build a WHOOP V5 command frame (offline)")
    p.add_argument("seq", type=int)
    p.add_argument("cmd", type=int)
    p.add_argument("payload", nargs="?", default="")
    p.set_defaults(func=cmd_frame)

    p = sub.add_parser("commands", help="list the reverse-engineered command catalog")
    p.set_defaults(func=cmd_commands)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
