# whoop-ble

A tiny, dependency-light toolkit for talking to a **WHOOP 5.0** band
directly over Bluetooth LE — no WHOOP app, no WHOOP servers, no Xcode.

All protocol knowledge here is reverse-engineered, primarily by the
[**Goose**](https://github.com/b-nnett/goose) project (archived alpha), which
mined the WHOOP Android APK and built on
[OpenWHOOP](https://github.com/pywebguru/OpenWHOOP) (the WHOOP 4.0 effort).
This repo distills that into one README, one CLI, and a set of bridges that
make it usable against a real band.

> **Not affiliated with WHOOP.** Product name is used only to describe
> compatibility. Writing commands to your own health device is at your own
> risk — everything here is reverse-engineered, not documented or supported
> by the manufacturer.

---

## First principles: what a WHOOP 5.0 actually is (BLE-wise)

A WHOOP 5.0 is a Bluetooth LE peripheral with two tiers of services:

1. **Public, standard GATT services** — heart rate (`0x180D`), battery
   (`0x180F`), device info (`0x180A`). Any device can connect and read
   these. No pairing required.
2. **A locked custom WHOOP service** — where everything interesting lives:
   the command channel, live vitals streams, alarms/haptics, and the
   historical data store. Subscribing or writing from an **unbonded**
   central returns GATT `Insufficient Authentication`.

Two more facts complete the picture:

- The band accepts **one connection at a time**, and stops advertising
  while connected. If the phone's WHOOP app is holding it, nobody else
  can even see it.
- The interesting data doesn't come for free: the standard heart-rate
  characteristic stays **silent** until a client sends the enable command
  through the locked custom service.

So the whole game is: **get a client onto the custom service.** That
requires either being already bonded, or completing a pairing exchange
with the band.

## Why the bridges exist

Every direct route to the custom service was tried and hit a wall. The
bridges exist because of these specific, empirical failures — each bridge
is the workaround for one of them:

| Route | What happened |
|---|---|
| **Direct from this PC** (Python + bleak) | Connects fine; standard reads work; custom service returns *Insufficient Authentication*. Needs a bond. |
| **Pairing the band with Windows** | Fails mid-exchange every time ("try connecting again"); the registry stores the device name but **no LTK** — the SMP key exchange never completes. Cleared stale records, reset the radio, retried: same. Why it fails is unknown (opaque stack). |
| **PC browser + Web Bluetooth** (the "obvious" dashboard route) | Chrome on Windows *has* Web Bluetooth — but it's the same wall with a nicer API: Web Bluetooth has **no pairing API**, and Windows never auto-initiates SMP when an encrypted characteristic is touched. The browser hits *Insufficient Authentication* exactly like Python did. |
| **Phone browser + Web Bluetooth** | The phone *is* bonded (the WHOOP app paired it at OS level) — but the author's browsers ship without Web Bluetooth: Brave has it **compiled out** (flag page: "not available on your platform"), Chrome reports "globally disabled", Firefox never had it. Bond but no API. |
| **Phone native app (Android BLE API)** | ✅ **The phone is bonded AND has a full BLE API.** No pairing exchange needed — the existing bond covers the encrypted link. This is the route that works, and is where every "verified" result below comes from. |
| **Linux host (BlueZ)** | Untested. `bluetoothctl` pairing would show the full SMP exchange — the one diagnostic that answers *why* Windows fails, and if it succeeds, that box becomes a permanent bonded gateway. Kept ready as `bridges/pi_gateway.py`. |

The pattern in one line: **every machine with the bond lacks an API, and
every machine with an API lacks the bond — except the phone.** So the
architecture is: whatever machine holds a usable radio+bond becomes a
"dumb BLE modem", and it exposes **one small HTTP interface** that
everything else (curl, scripts, agents, a future dashboard) talks to.
That interface is identical across all bridges, so the brains — frame
building, the command catalog, decoding — always live on the PC in
`whoop_ble.py`.

```
band ⇄ (phone app | phone browser | linux gateway) ⇄ HTTP API ⇄ your PC
```

---

## Status: verified against a real band

All "verified" rows were confirmed live (2026-11, via the Android bridge
on the author's bonded phone; band firmware `50.43.2.0`, HW `WG50_r52`,
name `WHOOP 5AG0047324`, MTU 247).

| Capability | Status |
|---|---|
| Scan / connect / standard GATT reads (PC-direct) | ✅ works |
| **Custom service access via the phone's bond** | ✅ works — hello, streams, alarms, storage pointers all confirmed |
| Handshake (`get_hello`) | ✅ returns serial number + device key hash + current Unix timestamp |
| Live vitals stream (`toggle_realtime_hr`) | ✅ 1 Hz type-40 realtime frames on `fd4b0005` |
| **Alarms + haptics** (`set_alarm_time` + `run_alarm`) | ✅ **physically felt on a wrist** — band reported `SUCCESSFUL_PLAY` |
| Battery pack info (`get_battery_pack_info`) | ✅ returns pack serial `WBB5AP0606717` |
| Historical storage pointers (`get_data_range`) | ✅ returns page pointers (ring-buffer pages) |
| Bulk historical download (`send_historical_data` flow) | ⏳ pointers work; the pull dance is scripted but not yet exercised |
| Standard `0x2A37` heart rate | ⚠️ subscribes, stays silent on this band — live HR arrives via the custom type-40 frames instead |
| **Tap detection** | ✅ confirmed — `DOUBLE_TAP` arrives as event frames (type 48, event id 14) |
| Optical debug reads (`40/42/44`), ext. battery (`98`), research packet (`132`) | ⛔ band replies `UNSUPPORTED` on this firmware |
| Body location (`84`) | ❌ replies `FAILURE` — likely needs a revision payload byte; format TBD |
| Clock write/read (`10`/`11`) | ⏳ stalled the ATT write-ACK ~10 s when tried; unverified |
| Packet semantics (sleep stages, SpO2, temp, strain) | 🕳️ undecoded upstream (Goose TODO) |

Operational quirks discovered live:

- **Alarm slots are 1-based** — slot 0 returns `INVALID_ALARM_ID`; slot 1
  works. Waveform `[47,152]` is a valid effect pair.
- Some commands (`63`, `11`, `98`) make the band **delay the ATT
  write-response ~10 s**. The Android app has a write watchdog that
  recovers; expect this when scripting.
- The band streams battery-pack telemetry unprompted (puffin event frames
  carrying the pack serial).
- Band clock is Unix-epoch synced (frame timestamps are Unix seconds).

---

## Quick start

The fastest way to see anything work:

```sh
pip install bleak
python.exe whoop_ble.py dashboard      # → http://localhost:8000
```

The dashboard figures out the rest by itself:

- **A bridge is reachable** (phone app over adb forward, web bridge,
  pi_gateway — any of them)? → full mode: every panel unlocked — live
  decoded telemetry, the whole command catalog, haptics, historical grab.
- **No bridge?** → it falls back to **direct BLE mode** on the PC's own
  radio: scans, connects, streams the public GATT stuff (device info,
  battery) — and the Bridge panel shows step-by-step instructions for
  lighting up each bridge, with a URL box to connect to it at runtime.
  No restart needed, no guessing why things are locked — the band's own
  `Insufficient Authentication` refusals stream right into the log.

For the individual CLI commands (useful without a browser):

```sh
python.exe whoop_ble.py scan          # find the band (must not be connected to phone)
python.exe whoop_ble.py info          # connect: service map + device info + battery
python.exe whoop_ble.py hello         # full probe: subscribe all + send client hello
python.exe whoop_ble.py commands      # list the command catalog
python.exe whoop_ble.py send get_hello
python.exe whoop_ble.py frame 1 3 01  # offline: build a frame, print hex
```

All BLE subcommands accept `--seconds`, `--scan`, and `--address`.
Offline subcommands (`frame`, `commands`, `send --dry-run`) need no
Bluetooth at all.

To get **past the authentication gate**, use one of the bridges below.

---

## Protocol reference

### BLE endpoints

Standard services are open; the WHOOP custom service requires a bond.
Characteristic UUIDs are the service UUID with `0002` (command, write) and
`0003`/`0004`/`0005`/`0007` (notifications) suffixes:

| Role | V5 UUID | gen4 UUID |
|---|---|---|
| Service | `fd4b0001-cce1-4033-93ce-002d5875f58a` | `61080001-8d6d-82b8-614a-1c8cb0f8dcc6` |
| **Command (write)** | `fd4b0002-...-585a` | `61080002-...-dcc6` |
| Notify: command responses | `fd4b0003-...-585a` | `61080003-...-dcc6` |
| Notify: events/telemetry | `fd4b0004-...-585a` | `61080004-...-dcc6` |
| Notify: realtime data | `fd4b0005-...-585a` | `61080005-...-dcc6` |
| Notify: debug menu | `fd4b0007-...-585a` | `61080007-...-dcc6` |

Standard services: `0x1800` GAP, `0x1801` GATT, `0x180D` Heart Rate
(`0x2A37` notify), `0x180A` Device Info (`2A24/25/26/27/28/29` read),
`0x180F` Battery (`0x2A19`).

### Frame format (V5)

```
byte:   0     1     2-3       4     5     6-7         8 ...        ...end
       0xAA  0x01  len u16le  0x00  0x01  CRC16 u16le  payload      CRC32 u32le
       |---- header, 8 bytes ----|    hdr CRC         |---- len-4 bytes ----|

payload = [packet_type, seq, command, data..., zero-pad to 4-byte alignment]
len     = len(payload) + 4          (payload + payload CRC32)
CRC16   = Modbus variant (poly 0xA001, init 0xFFFF), over first 6 header bytes, LE
CRC32   = standard zlib CRC32, over padded payload, LE
```

Known-good example — the client hello (seq 1, command 145, payload `01`),
captured from the official app and reproduced byte-for-byte by
`whoop_ble.py`:

```
aa0108000001e671 230191 01 363e5c8d
header + hdr CRC |type,seq,cmd| data | payload CRC32
```

### Packet types (first payload byte)

| # | Name | Direction |
|---|---|---|
| 35 | command | client → band |
| 36 | command_response | band → client |
| 37 / 38 | puffin command / response | "Puffin" = WHOOP hardware family |
| 40 / 43 | realtime data / raw data | band → client |
| 47 | historical data | band → client (bulk sync) |
| 48 | event | band → client |
| 49 | metadata | band → client (`1` HistoryStart, `2` HistoryEnd, `3` HistoryComplete) |
| 50 | console logs | band → client |
| 51 / 52 | realtime / historical IMU stream | band → client |
| 56 | puffin metadata | band → client |

### Command responses (type 36) — decoded live

```
[36, respSeq, cmd, echoedClientSeq, resultCode, body...]
```

| resultCode | Meaning |
|---|---|
| 0 | FAILURE |
| 1 | SUCCESS |
| 2 | PENDING |
| 3 | UNSUPPORTED |

**Alarm/haptics family** (cmds 66–69): body = `[echoedRevision,
hapticsStatus, ...]`, with status codes:

| # | Status | # | Status |
|---|---|---|---|
| 0 | UNKNOWN | 6 | HAPTICS_FAILURE |
| 1 | VALID_PATTERN | 7 | HAPTICS_TIMEOUT |
| 2 | INVALID_EFFECT | 8 | HAPTICS_BUSY |
| 3 | INVALID_LOOPS | 9 | HAPTICS_STOPPED |
| 4 | INVALID_DURATION | 10 | INVALID_ALARM_TIME |
| 5 | **SUCCESSFUL_PLAY** | 11 | INVALID_ALARM_ID |

Other observed live responses: `get_hello` returns band serial (ASCII),
a device key hash, and a Unix timestamp; `get_battery_pack_info` returns
the pack serial; `get_data_range` returns u32le page pointers
(current/oldest/end/behind) — e.g. `103232, 103268, 103313, 103268 (+19)`.

### Strap events (type 48) — name table (from Goose `protocol.rs`)

Event frames carry `event_id` u16le at frame[10], Unix timestamp at
frame[12] (both verified live). Event id **14 = DOUBLE_TAP** was observed
in the wild — the author tapped the band while chatting and the frames
arrived one per tap:

| id | Event | id | Event |
|---|---|---|---|
| 0 | UNDEFINED | 13 | RTC_LOST |
| 1 | ERROR | 14 | **DOUBLE_TAP** |
| 2 | CONSOLE_OUTPUT | 15 | BOOT |
| 3 | BATTERY_LEVEL | 16 | SET_RTC |
| 4 | SYSTEM_CONTROL | 17 | TEMPERATURE_LEVEL |
| 7 | CHARGING_ON | 18 | PAIRING_MODE |
| 8 | CHARGING_OFF | 28 | FLASH_INIT_COMPLETE |
| 9 | WRIST_ON | 29 | STRAP_CONDITION_REPORT |
| 10 | WRIST_OFF | 33/34 | BLE_REALTIME_HR_ON/OFF |
| 11 | BLE_CONNECTION_UP | 56–60 | alarm lifecycle / HAPTICS_FIRED |
| 12 | BLE_CONNECTION_DOWN | 63/96–98/100/109/123 | battery / sync / firmware events |

### Data packets (types 40/43/47/51/52) — domain table

Data-packet frames carry `packet_k` at frame[10], status at frame[11],
counter/page u32le at frame[12], timestamp u32le at frame[16] (layout from
Goose `parse_data_packet_payload`). Known `packet_k` domains: `9/12/18/24`
normal history (HR-marked), `10/21` raw motion, `16` raw ECG, `17` R17
optical-filtered, `19/22` research, `25/26` pulse information, `7/20`
legacy/counted. Our live 1 Hz stream shows `packet_k = 80` (domain unknown —
likely the realtime vitals domain; not in Goose's table).

### Command catalog

Frames are `[35, seq, cmd, payload...]`; names match `whoop_ble.py send`.
"Verified" = exercised against the author's band.

**Handshake & live vitals**

| # | CLI name | Payload | What it does | |
|---|---|---|---|---|
| 145 | `get_hello` | `01` | handshake | ✅ verified |
| 3 | `toggle_realtime_hr` | `01` on / `00` off | live vitals stream (1 Hz type-40 frames) | ✅ verified |
| 63 | `send_r10_r11_realtime` | `01` / `00` | R10/R11 realtime stream | ⏳ ACK slow |
| 106 | `toggle_imu_mode` | `0101` / `0100` | IMU/accelerometer stream | untested |
| 107 | `enable_optical_data` | `0101` / `0100` | optical/PPG data | untested |
| 108 | `toggle_optical_mode` | `0101` / `0100` | optical sensor mode | untested |
| 153/154 | `toggle_persistent_r20/r21` | `0101` / `0100` | persistent sensor modes | untested |

**Historical sync (page-pointer protocol)**

| # | CLI name | Payload | What it does | |
|---|---|---|---|---|
| 34 | `get_data_range` | — | storage page pointers | ✅ verified |
| 22 | `send_historical_data` | — | stream stored pages (type-47 bursts) | untested |
| 23 | `historical_data_result` | `0100000000000000` | ack receipt; advances pointers | untested |
| 96 | `enter_high_freq_sync` | `02` + u16le×2 | fast sync mode | untested |
| 97 | `exit_high_freq_sync` | — | leave fast sync | untested |

**Clock & alarms**

| # | CLI name | Payload | What it does | |
|---|---|---|---|---|
| 11 | `get_clock` | — | read strap clock | ⏳ ACK slow |
| 10 | `set_clock` | u32le sec + u32le subsec | set strap clock | untested |
| 67 | `get_alarm_time` | `04` + id | read alarm (empty slots: no data) | ✅ ACKed |
| 66 | `set_alarm_time` | `04` id u32sec u16subsec + 8-byte waveform + u16le loop + u8 overall + u8 duration | set alarm | ✅ verified |
| 68 | `run_alarm` | `02` + id | play it now | ✅ **felt it** |
| 69 | `disable_alarm` | `0200ff` | disable all alarms | untested |

WHOOP-default haptic pattern: waveform `[47,152,0,0,0,0,0,0]`, loop 0,
overall 7, duration 30 s. **Alarm IDs are 1-based.**

**Debug / research reads**

| # | CLI name | Payload | | |
|---|---|---|---|---|
| 84 | `get_body_location_and_status` | — | ❌ FAILURE (payload format TBD) | |
| 98 | `get_extended_battery_info` | — | ⛔ UNSUPPORTED | |
| 151 | `get_battery_pack_info` | `01` | ✅ pack serial | |
| 40/42/44 | `get_led_drive` / `get_tia_gain` / `get_bias_offset` | — | ⛔ UNSUPPORTED | |
| 121 | `get_device_config_value` | `01` + 32-byte key | untested (no known keys) | |
| 128 | `get_feature_flag_value` | `01` + 32-byte key | untested | |
| 105 | `toggle_imu_mode_historical` | explicit payload | untested | |
| 132 | `get_research_packet` | — | ⛔ UNSUPPORTED | |

---

## The bridges

All three expose the **same HTTP interface** — `/state`, `/log?since=N`,
`/write` (`{"frame":"<hex>"}`) or `/cmd/<catalog-name>`, `/reset`, `/help` —
and all frames are built/decoded PC-side by `whoop_ble.py`. Only the
transport differs. Pick whichever one can reach the band with a bond.

### 1. Native Android bridge (`bridges/android-bridge/`) — the working route

Exists because: the phone has the bond and a full BLE API; no pairing
needed. This is how every ✅ above was earned.

```
phone app (Android BLE + loopback HTTP) <— adb forward —> curl on the PC
```

A complete, zero-dependency Android Studio project (one `MainActivity.kt`,
no external libraries, programmatic UI). Connects, subscribes to all WHOOP
notification characteristics + standard HR, exposes the HTTP surface on
`127.0.0.1:8443` (loopback only — nothing on the LAN can touch the band).

Build & run (pins AGP 8.10.1 / Gradle 8.14 / Kotlin 2.1.20, compileSdk 35,
minSdk 31):

1. Android Studio → Open → `whoop-ble/bridges/android-bridge`
2. Plug phone in (USB debugging on) → **Run** → grant the two Bluetooth
   permissions → tap **CONNECT TO WHOOP**
3. Before connecting: force-stop the WHOOP app — it holds the band's only
   connection slot. (`adb shell am force-stop com.whoop.android`; if its
   sticky service restarts and re-grabs the band:
   `adb shell pm disable-user --user 0 com.whoop.android`, re-enable with
   `pm enable` afterwards.)
4. On the PC (each time the cable is re-plugged):
   ```sh
   adb.exe forward tcp:8443 tcp:8443      # NOT reverse — see below
   curl.exe -s http://localhost:8443/state
   ```
   `adb forward` = PC listens on 8443, tunnels to the phone.
   (`adb reverse` is the opposite direction and will fail — the phone's
   8443 is held by the app itself.)

Notes: the app has a **write watchdog** (recovers from the band's
occasional ~10 s ATT write-response stalls) and keeps all state in the
companion object (survives activity recreation). It can be built from the
CLI without Studio:
`java -cp gradle\wrapper\gradle-wrapper.jar org.gradle.wrapper.GradleWrapperMain assembleDebug`
using the Studio JBR, then `adb install -r app\build\outputs\apk\debug\app-debug.apk`.

### 2. Phone-browser bridge (`bridges/bridge_server.py`) — same idea, via Chrome

Exists because: *some* phones have browsers with Web Bluetooth — and if
yours does, this avoids installing any app. Stdlib-only HTTPS server on
the PC + embedded Web Bluetooth page:

```
phone Chrome (Web Bluetooth) <— HTTPS + long-poll —> bridge_server.py <— HTTP —> you
```

The phone page connects, subscribes everything, and forwards bytes both
ways. HTTPS with a self-signed cert is mandatory (Web Bluetooth requires
a secure context); `certs/` is gitignored because it contains a private
key — if missing, the server prints the exact `openssl` command to
generate one, and the startup banner prints the phone URL, phone steps,
and control examples. Phone and PC must share a LAN.

**Caveat (why it's not the default):** the author's phone browsers all
lack Web Bluetooth (Brave: compiled out; Chrome: globally disabled;
Firefox/iOS: never had it). Sanity-check yours at
https://googlechrome.github.io/samples/web-bluetooth/device-info.html
before using this route.

### 3. Linux gateway (`bridges/pi_gateway.py`) — the pairing front

Exists because: BlueZ is the only stack that will *show us* the SMP
exchange. For a Raspberry Pi, spare Linux box, or **live-USB Ubuntu on
this very PC** (the Intel radio is BlueZ-compatible).

- `pair` — drives `bluetoothctl` through the full pairing dance with
  every SMP line streamed live. If it succeeds, that box is bonded forever
  and becomes a standalone gateway; if the band refuses, we finally know
  *why* Windows pairing fails too.
- `serve` — direct BLE gateway with the same HTTP surface, auto-reconnect,
  plain HTTP on the LAN. If subscriptions fail with auth errors, `/state`
  says so and points at `pair`.

```sh
sudo apt install -y bluez python3-pip && pip install bleak
python3 bridges/pi_gateway.py pair    # first, once
python3 bridges/pi_gateway.py serve   # then (systemd unit below)
```

(`serve` must run on the Linux box itself — the radio must be local; it
does not work under WSL.) Optional boot service,
`/etc/systemd/system/whoop-gateway.service`:

```ini
[Unit]
Description=WHOOP BLE gateway
After=bluetooth.target

[Service]
WorkingDirectory=/home/pi/whoop-ble
ExecStart=/usr/bin/python3 bridges/pi_gateway.py serve --address C2:51:BC:8F:B2:28
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## The dashboard (`dashboard.py`, via `whoop_ble.py dashboard`)

The browser control room — speak to it at **http://localhost:8000**.

**Two modes, auto-selected at startup, hot-swappable at runtime** via the
Bridge panel (URL box + Connect/Direct buttons):

| | Bridge mode | Direct mode |
|---|---|---|
| When | a bridge answers at `--bridge` (default `http://localhost:8443`) | nothing reachable (or `--bridge none`) |
| Radio | the bridge's (phone's bond / Pi's bond) | this PC's bleak radio |
| Catalog | ✅ all 29 commands | 🔒 locked — no bond on the PC |
| Public GATT (battery, device info) | ✅ | ✅ streams automatically on connect |
| The wall, made visible | — | the band's `Insufficient Authentication` refusals are logged live |

Bridge auto-detection: the phone app speaks plain HTTP and the web bridge
speaks HTTPS (self-signed), often on the same port — the dashboard probes
both schemes and adopts whichever answers. `pi_gateway` works the same
way (`http://<pi-ip>:8443`). Switching back to direct frees the band for
the bridge and vice versa.

**Panels:**

- **Device** — band name + the public reads as a card (serial, firmware,
  hardware, manufacturer, battery)
- **Bridge** — current mode, URL connect box, and (in direct mode)
  step-by-step unlock instructions for all three bridges
- **Live stream** — every event decoded server-side: strap events get
  *names* (`DOUBLE_TAP`, `WRIST_ON`, `HAPTICS_FIRED`, …), command responses
  get result codes + haptics statuses, data packets get domains and
  page/timestamp fields
- **Command console** — the catalog as color-grouped buttons; anything
  state-changing asks for confirmation first
- **Haptics lab** — 8 waveform-slot composer → `set_alarm_time` +
  optional immediate `run_alarm` (the buzz feature, one click)
- **■ STOP ALL STREAMS** — the full `stopPhysiologyCapture` sequence
  (7 commands, everything OFF) in one confirmed click
- **Historical grab** — walks the page-pointer dance
  (`get_data_range` → `send_historical_data` → collect type-47 pages →
  ack each HistoryEnd → stop on HistoryComplete or 30 s idle) and offers
  the capture as a JSON download (bridge mode only)

API (also usable directly): `/api/catalog`, `/api/state`, `/api/log?since=N`
(enriched), `/api/send {name,payload}`, `/api/frame {frame}`,
`/api/haptics {waveform,…,run_now}`, `/api/stop`, `/api/backend {url}`
(mode switching), `/api/capture/start|status|download`.

---

## CLI reference (`whoop_ble.py`)

| Subcommand | What it runs | Key functions |
|---|---|---|
| `scan [--seconds N] [--all]` | find advertising WHOOPs | `cmd_scan`, `looks_like_whoop` |
| `info` | connect + service map + device info | `cmd_info`, `print_service_map`, `log_standard_info`, `find_char` |
| `hr [--seconds N]` | subscribe standard heart rate | `cmd_hr`, `parse_hr_measurement` |
| `hello [--seconds N]` | subscribe everything + send hello | `cmd_hello`, `describe_frame` |
| `send NAME [--payload H] [--seq N] [--dry-run]` | send a catalog command | `cmd_send`, `build_v5_frame` |
| `frame SEQ CMD [PAYLOAD_HEX]` | offline frame builder | `cmd_frame`, `crc16_modbus` |
| `commands` | list the catalog | `cmd_commands` |
| `dashboard [--bridge URL] [--port 8000]` | browser control room (implementation: `dashboard.py`) | `dashboard.main` |

Shared helpers: `crc16_modbus`, `build_v5_frame` (regression-tested against
the captured hello), `find_whoop`, `connect_to_whoop`, `parse_hr_measurement`,
`describe_frame`.

---

## Known unknowns / open research

- **Sleep stages, respiratory rate, SpO2, wrist temp, strain scores** — raw
  packets exist in the historical sync (type 47); semantics undecoded
  upstream (Goose `RemainingDataTodo.md`). Bulk historical pull not yet
  exercised end-to-end (pointers ✅, the type-47 dance is scripted).
- **Type-40 realtime frame fields** — 1 Hz stream confirmed; embedded
  values (HR candidate bytes, timestamps) partially identified, exact
  field layout TBD.
- **Tap/gesture detection** — event frames (type 48, candidate event id
  14) observed at suspicious times; tap-channel hypothesis untested.
- **`84` body-location payload format**; **clock commands** (slow-ACK,
  responses may arrive as events rather than type-36).

## Open problems

1. **Windows pairing fails mid-exchange** (no LTK). Parked — the Android
   bridge sidesteps it. The BlueZ `pair` attempt (Pi or live-USB) remains
   the diagnostic that would explain it and could unlock direct-PC access.
2. **Web Bluetooth availability is browser-build roulette** (see bridge #2).
3. Historical bulk download + payload decode (see open research).

## Credits

- **[Goose](https://github.com/b-nnett/goose)** — the WHOOP 5.0 BLE
  reverse-engineering (Swift + Rust PoC). Nearly every constant here comes
  from its source.
- **[OpenWHOOP](https://github.com/pywebguru/OpenWHOOP)** — the WHOOP 4.0
  reverse-engineering project Goose references.

## Upstream tracking (do this periodically)

Upstream is the source of truth — **check on it once in a while**:

- <https://github.com/b-nnett/goose> — currently **ARCHIVED** (read-only).
  If revived or forked, new WHOOP 5.0 discoveries land there first.
- Watch for: new command numbers, packet-semantics decoding
  (sleep/SpO2/temp), frame-format changes, anything about pairing flows.
- Integrate findings via the checklist in the agent appendix below.

---

## Appendix: for AI agents

This repo is expected to be read, traversed, and modified by AI agents.
This section is the orientation brief.

### Repo map (it's tiny — read everything)

```
whoop-ble/
├── README.md              ← you are here; protocol reference + this appendix
├── whoop_ble.py            ← protocol core + direct-PC CLI (catalog, frame
│                              builder, parsers, CRCs). Everything imports this.
├── dashboard.py            ← browser dashboard frontend to any bridge
│                              (launched via `whoop_ble.py dashboard`)
├── bridges/                ← every transport that turns a radio into a gateway:
│   ├── android-bridge/       ← ★ the working route (native app, adb forward)
│   ├── bridge_server.py      ← phone-browser route (needs Web Bluetooth)
│   └── pi_gateway.py         ← Linux/BlueZ route (pair + serve)
├── certs/                  ← self-signed HTTPS cert (gitignored, private key!)
└── .gitignore
```

All bridges share ONE interface (same endpoints, same catalog, same frame
format — only the transport differs). The response-format tables above
(result codes, haptics statuses) were decoded live; use them when reading
`/log` type-36 entries.

### Execution environment gotchas

- Host is **WSL on Windows**. Bluetooth belongs to Windows: BLE commands
  run under **Windows Python** (`python.exe …` from the repo dir; it's a
  Dropbox path visible to both sides). WSL's `python3` can't reach the
  radio and has no bleak. Windows Python 3.14 has bleak installed.
- The band is at `C2:51:BC:8F:B2:28` (as of writing; addresses can rotate —
  `scan` rather than hardcode).
- **Phone-bridge driving:** ensure the app is running on the phone and
  `adb.exe forward tcp:8443 tcp:8443` is active (adb lives in the Android
  SDK: `C:\Users\<user>\AppData\Local\Android\Sdk\platform-tools\adb.exe`;
  re-run forward after re-plugging the cable; **forward**, not reverse).
  Then plain HTTP: `curl.exe -s http://localhost:8443/state`,
  `/log?since=N`, `-X POST http://localhost:8443/write -d '{"frame":"<hex>"}'`.
- Build frames with `python.exe whoop_ble.py frame SEQ CMD [PAYLOAD]` or
  `send <name> --dry-run`. Beware: **`curl.exe` inside a
  `while read` loop eats the loop's stdin** — redirect with `< /dev/null`
  or drive sweeps from a Python script instead.
- The WHOOP app (`com.whoop.android`) auto-restarts and steals the band;
  force-stop it, or `pm disable-user --user 0 com.whoop.android` if it's
  sticky (re-enable with `pm enable` when done). Band data keeps
  recording on-flash and syncs later — no data loss.
- **Linux route:** `bridges/pi_gateway.py` must run on the Linux box
  itself (not WSL). `pair` first (watch the SMP lines), then `serve`;
  control from the PC with `curl http://<linux-ip>:8443/...`.
- Leftover port-8443 listeners on the PC (stale bridge servers) can be
  killed via `Get-NetTCPConnection -LocalPort 8443` → `Stop-Process`.

### Where things live in `whoop_ble.py`

| Thing | Location |
|---|---|
| Service/characteristic UUIDs | module constants at top |
| Command catalog | `COMMAND_CATALOG` |
| Frame format + CRCs | `crc16_modbus`, `build_v5_frame` |
| Frame peek | `describe_frame` |
| HR parsing | `parse_hr_measurement` |
| Scan/connect | `find_whoop`, `connect_to_whoop` |
| Subcommand handlers | `cmd_scan`, `cmd_info`, `cmd_hr`, `cmd_hello`, `cmd_send`, `cmd_frame`, `cmd_commands` |

### Ground truth you must not break

- `build_v5_frame(1, 145, b"\x01")` MUST produce
  `aa0108000001e67123019101363e5c8d`. Regression test:
  `python.exe whoop_ble.py frame 1 145 01`.
- All constants come from Goose (see Upstream tracking). Do not invent
  command numbers or payloads; only add what upstream documents.

### Integrating new upstream findings (checklist)

1. Find the constant in Goose source:
   - Command numbers & payloads: `GooseSwift/GooseBLEClient.swift`
     (enums `SensorStreamCommandKind`, `HistoricalCommandKind`,
     `AlarmCommandKind`, `ClockCommandKind`, `debugResearchCommandDefinitions`)
   - Packet types & parsing: `Rust/core/src/protocol.rs`
   - Undecoded semantics: `docs/goose-swift-mvp/RemainingDataTodo.md`
   - Alarm/haptics response parsing: `GooseBLEClient+Parsing.swift`
     (`commandResultName`, `hapticsAlarmStatusName`)
2. Add to `COMMAND_CATALOG` in `whoop_ble.py`
   (`"name": (number, default_payload_hex_or_None, "description")`).
3. Add to the README catalog tables (with a verdict column).
4. New packet type → `PACKET_TYPE_NAMES` + README packet table.
5. Verify offline (`commands`, `send <name> --dry-run`).
6. Live-test via the phone bridge if available; record the type-36
   response in the README response tables.
7. Update the status table.

### Live-testing procedure (band present)

1. Band advertising? `python.exe whoop_ble.py scan --seconds 20`. If not:
   the WHOOP app holds it (see gotchas above).
2. Phone bridge up? `curl.exe -s http://localhost:8443/state` →
   `connected: true`. If not: user opens the app and taps CONNECT.
3. Optional but pleasant: `python.exe whoop_ble.py dashboard` and drive
   from the browser at http://localhost:8000 (decodes events/responses
   using the README name tables). It auto-picks bridge vs direct mode;
   `POST /api/backend {"url": …}` switches at runtime ("none" = direct
   BLE). If the running dashboard predates a code edit, kill port-8000
   listeners and restart — stale servers serving old routes cause
   confusing "not found"s.
4. Or by hand: send `POST /cmd/hello`; watch `/log` for the type-36 pair
   (serial + key hash). This is the handshake sanity check.
4. Reads are safe (`get_*`). Some commands stall the ATT write-ACK
   ~10 s — the app's watchdog recovers; don't re-send immediately.
5. PC-direct (`whoop_ble.py hello`) is only useful for the standard
   services; the custom service will always fail with *Insufficient
   Authentication* from this PC (no bond — see "Why the bridges exist").
6. The dashboard enriches `/api/log` with the name tables above — if a new
   event id or result code shows up raw (e.g. `event_207`), add it to the
   table in `dashboard.py` **and** the README.

### Safety rules for agents

- Never send `set_*`/state-changing commands (clock, alarms, sensor
  toggles) without explicit user confirmation for that specific command.
- Reads (`get_*`) and the hello are safe.
- `toggle_*` commands change what the band is streaming — confirm first,
  and always know the matching OFF payload.
- `disable_alarm` (`0200ff`) disables **all** the user's alarms — never
  send it without explicit consent.
- Do not remove or alter the disclaimers (not affiliated / at your own
  risk).
