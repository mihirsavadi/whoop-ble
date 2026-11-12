# whoop-ble

A tiny, dependency-light Python toolkit for talking to a **WHOOP 5.0** band
directly over Bluetooth LE — no WHOOP app, no WHOOP servers, no Xcode.

All protocol knowledge here is reverse-engineered, primarily by the
[**Goose**](https://github.com/b-nnett/goose) project (archived alpha), which
mined the WHOOP Android APK and built on
[OpenWHOOP](https://github.com/pywebguru/OpenWHOOP) (the WHOOP 4.0 effort).
This repo just distills that into one README and one CLI file so the
protocol is usable from any BLE-capable machine.

> **Not affiliated with WHOOP.** Product name is used only to describe
> compatibility. Writing commands to your own health device is at your own
> risk — everything here is reverse-engineered, not documented or supported
> by the manufacturer.

---

## Status at a glance (verified against a real band)

| Capability | Status |
|---|---|
| Scan / connect | ✅ works (band only advertises when the phone app is *not* connected) |
| Service map dump | ✅ works |
| Device info (model, firmware, HW rev, serial, manufacturer) | ✅ works — no pairing |
| Battery level | ✅ works — no pairing |
| Heart-rate subscribe (standard GATT) | ✅ subscribes, but band stays **silent** until the custom-service enable command is sent |
| WHOOP custom service (commands, streams, historical sync) | 🔒 blocked by **BLE bonding** — "Insufficient Authentication" from an unbonded client. Pairing with Windows is currently failing mid-exchange (no LTK stored); pending |
| Packet semantics (sleep stages, SpO2, temp, strain scores) | 🕳️ undecoded upstream (Goose TODO) |

Hardware observed live: model `5.0`, firmware `50.43.2.0`, hardware `WG50_r52`,
advertised name `WHOOP 5AG0047324`, MTU 247.

---

## Quick start

Requires Python 3.9+ and [`bleak`](https://github.com/hbldh/bleak):

```sh
pip install bleak
```

On Windows (from WSL, use the Windows Python so the radio is visible):

```sh
python.exe whoop_ble.py scan          # find the band (phone Bluetooth must be OFF)
python.exe whoop_ble.py info          # connect: service map + device info + battery
python.exe whoop_ble.py hr            # live heart rate (needs band stream enabled)
python.exe whoop_ble.py hello         # full probe: subscribe all + send client hello
python.exe whoop_ble.py commands      # list the reverse-engineered command catalog
python.exe whoop_ble.py send get_hello           # send a catalog command
python.exe whoop_ble.py send toggle_realtime_hr  # enable live HR streaming
python.exe whoop_ble.py frame 1 3 01  # offline: build a frame, print hex
```

All BLE commands accept `--seconds` (listen time), `--scan` (scan duration),
and `--address XX:XX:...` (skip discovery).

**Key gotcha:** the band stops advertising while connected to the phone's
WHOOP app. Turn phone Bluetooth off (or airplane mode) before scanning.

**Pairing gate:** the WHOOP custom service requires an encrypted/bonded
link. Standard GATT (heart rate, battery, device info) is open. Pairing the
band with Windows currently fails mid-exchange ("try connecting again", no
LTK stored) — tracked below under *Open problems*.

---

## BLE endpoints

### Standard GATT services (open, no pairing)

| Service | Characteristic | Props | Purpose |
|---|---|---|---|
| `0x1800` Generic Access | `0x2A00`, `0x2A01`, `0x2AA6` | read | device name, appearance |
| `0x1801` Generic Attribute | `0x2A05` | indicate | service changed |
| `0x180D` Heart Rate | `0x2A37` | **notify** | live BPM + RR intervals (RR in 1/1024 s units) |
| `0x180A` Device Info | `0x2A24`/`2A25`/`2A26`/`2A27`/`2A28`/`2A29` | read | model, serial, firmware, HW/SW rev, manufacturer |
| `0x180F` Battery | `0x2A19` | read+notify | battery % |

Live heart rate over `0x2A37` stays **silent** until the band is told to
stream (command `toggle_realtime_hr`, #3, via the custom service below).

### WHOOP custom services (pairing required)

The band exposes the V5 ("Goose") service; older/gen4-era UUIDs are kept for
compatibility. Characteristic UUIDs are service UUIDs with `0002` (command,
writable) and `0003`/`0004`/`0005`/`0007` (notifications) suffixes:

| Role | V5 UUID | gen4 UUID |
|---|---|---|
| Service | `fd4b0001-cce1-4033-93ce-002d5875f58a` | `61080001-8d6d-82b8-614a-1c8cb0f8dcc6` |
| **Command (write)** | `fd4b0002-...-585a` | `61080002-...-dcc6` |
| Notify: command responses | `fd4b0003-...-585a` | `61080003-...-dcc6` |
| Notify: data | `fd4b0004-...-585a` | `61080004-...-dcc6` |
| Notify: data | `fd4b0005-...-585a` | `61080005-...-dcc6` |
| Notify: debug menu | `fd4b0007-...-585a` | `61080007-...-dcc6` |

Subscribing to any of these from an unbonded central returns GATT
**"Insufficient Authentication"** — the single gate to everything below.

---

## Frame format (V5)

All writes to the command characteristic are frames with a CRC-protected
header and payload:

```
byte:   0     1     2-3       4     5     6-7         8 ...        ...end
       0xAA  0x01  len u16le  0x00  0x01  CRC16 u16le  payload      CRC32 u32le
       |---- header, 8 bytes ----|    hdr CRC         |---- len-4 bytes ----|

payload = [packet_type, seq, command, data..., zero-pad to 4-byte alignment]
len     = len(payload) + 4          (payload + payload CRC32)
CRC16   = Modbus variant (poly 0xA001, init 0xFFFF), over first 6 header bytes, little-endian
CRC32   = standard zlib CRC32, over padded payload, little-endian
```

Known-good example — the client hello sent at connect time (seq 1,
command 145, payload `01`):

```
aa0108000001e671 230191 01 363e5c8d
header + hdr CRC |type,seq,cmd| data | payload CRC32
```

### Packet types (first payload byte)

| # | Name | Direction |
|---|---|---|
| 35 | command | client → band |
| 36 | command_response | band → client |
| 37 / 38 | puffin command / response | "Puffin" = WHOOP internal hardware family |
| 40 / 43 | realtime data / raw data | band → client |
| 47 | historical data | band → client (bulk sync) |
| 48 | event | band → client |
| 49 | metadata | band → client |
| 50 | console logs | band → client |
| 51 / 52 | realtime / historical IMU stream | band → client |
| 56 | puffin metadata | band → client |

### Metadata sub-types (packet type 49 payloads, u16le)

`1` = HistoryStart, `2` = HistoryEnd, `3` = HistoryComplete — bracket the
bulk historical transfer.

---

## Command catalog

Send as frames (above): packet_type 35, sequence byte (per-connection,
starts at 1), command number, payload. Names below match the CLI
(`whoop_ble.py send <name>`).

### Handshake & live vitals

| # | CLI name | Payload (hex) | What it does |
|---|---|---|---|
| 145 | `get_hello` | `01` | handshake; reproduces the known-good hello frame |
| 3 | `toggle_realtime_hr` | `01` on / `00` off | enable live HR streaming (→ standard `0x2A37`) |
| 63 | `send_r10_r11_realtime` | `01` on / `00` off | realtime R10/R11 vitals stream |
| 106 | `toggle_imu_mode` | `0101` on / `0100` off | IMU/accelerometer stream (movement, steps) |
| 107 | `enable_optical_data` | `0101` on / `0100` off | optical/PPG sensor data |
| 108 | `toggle_optical_mode` | `0101` on / `0100` off | optical sensor mode |
| 153 / 154 | `toggle_persistent_r20` / `..._r21` | `0101` on / `0100` off | persistent sensor modes |

### Historical data sync (page-pointer protocol)

| # | CLI name | Payload | What it does |
|---|---|---|---|
| 34 | `get_data_range` | — | returns storage **page pointers**: current, oldest, end, pages-behind |
| 22 | `send_historical_data` | — | band streams stored pages (all-day HR, sleep, activities) |
| 23 | `historical_data_result` | `0100000000000000` | client ack; advances the pointers |
| 96 | `enter_high_freq_sync` | `02` + u16le interval + u16le duration | fast sync mode |
| 97 | `exit_high_freq_sync` | — | leave fast sync |

Flow: `get_data_range` → `send_historical_data` → receive packet type 47
bursts bracketed by HistoryStart/HistoryEnd/HistoryComplete metadata → ack
each burst with `historical_data_result`.

### Clock & alarms

| # | CLI name | Payload | What it does |
|---|---|---|---|
| 11 | `get_clock` | — | read strap clock |
| 10 | `set_clock` | u32le seconds + u32le subseconds | set strap clock |
| 67 | `get_alarm_time` | `04` + alarm id | read alarm |
| 66 | `set_alarm_time` | `04` id u32sec u16subsec + 8-byte waveform + u16le loop + u8 overall + u8 duration | set alarm incl. custom haptic waveform |
| 68 | `run_alarm` | `02` + id | trigger alarm now |
| 69 | `disable_alarm` | `0200ff` | disable all alarms |

WHOOP-default haptic pattern: waveform `[47,152,0,0,0,0,0,0]`, loop 0,
overall 7, duration 30 s.

### Debug / research reads

| # | CLI name | Payload | What it does |
|---|---|---|---|
| 84 | `get_body_location_and_status` | — | body placement / strap status |
| 132 | `get_research_packet` | — | generic research packet |
| 98 | `get_extended_battery_info` | — | battery beyond GATT |
| 151 | `get_battery_pack_info` | `01` | battery pack info (rev 1) |
| 40 / 42 / 44 | `get_led_drive` / `get_tia_gain` / `get_bias_offset` | — | optical front-end config |
| 121 | `get_device_config_value` | `01` + 32-byte key | config read by key |
| 128 | `get_feature_flag_value` | `01` + 32-byte key | feature flag read by key |
| 105 | `toggle_imu_mode_historical` | *required* | historical IMU toggle |

---

## CLI reference (`whoop_ble.py`)

One file; every function lives here and is listed below.

| Subcommand | What it runs | Key functions |
|---|---|---|
| `scan [--seconds N] [--all]` | find advertising WHOOPs | `cmd_scan`, `looks_like_whoop` |
| `info` | connect + service map + device info | `cmd_info`, `print_service_map`, `log_standard_info`, `find_char` |
| `hr [--seconds N]` | live heart rate | `cmd_hr`, `parse_hr_measurement`, `make_hr_handler` |
| `hello [--seconds N]` | subscribe everything + send hello | `cmd_hello`, `describe_frame`, `make_whoop_notification_handler` |
| `send NAME [--payload H] [--seq N] [--dry-run]` | send catalog command | `cmd_send`, `build_v5_frame` |
| `frame SEQ CMD [PAYLOAD_HEX]` | offline frame builder | `cmd_frame`, `crc16_modbus`, `build_v5_frame` |
| `commands` | list command catalog | `cmd_commands` |

Shared helpers: `crc16_modbus` (Modbus CRC16), `build_v5_frame` (verified
byte-for-byte against the captured hello frame), `find_whoop` (scan + match),
`connect_to_whoop` (scan + connect, exits with guidance if not found),
`parse_hr_measurement` (standard HR measurement incl. RR intervals),
`describe_frame` (best-effort WHOOP frame peek).

Offline subcommands (`frame`, `commands`, `send --dry-run`) work without
`bleak` or a Bluetooth adapter — useful for protocol experimentation.

---

## What's known-unknown (upstream decode TODO)

Even with a bonded link, these are **not decoded yet** (Goose
`RemainingDataTodo.md`): sleep stages, respiratory rate, SpO2, wrist
temperature, strain/recovery score inputs. The raw packets arrive in the
historical sync (type 47); their semantics are open research.

## Open problems

1. **Pairing with Windows fails mid-exchange.** UI says "try connecting
   again"; registry shows the device but no LTK. Stale record was cleared,
   still failing. Next steps: retry after radio toggle; try nRF Connect on
   the (already-bonded) phone; try a Linux host / Raspberry Pi where
   `bluetoothctl` shows the SMP exchange.
2. Everything behind the pairing gate is untested on this hardware.

## Credits

- **[Goose](https://github.com/b-nnett/goose)** — the WHOOP 5.0 BLE
  reverse-engineering (Swift + Rust proof of concept). Nearly every constant
  in this repo comes from its source.
- **[OpenWHOOP](https://github.com/pywebguru/OpenWHOOP)** — the WHOOP 4.0
  reverse-engineering project Goose references.
