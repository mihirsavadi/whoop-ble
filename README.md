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
| Handshake (`get_hello`, 145) | ✅ returns serial number + device key hash + current Unix timestamp |
| Live vitals stream (`toggle_realtime_hr`) | ✅ 1 Hz type-40 frames on `fd4b0005` |
| **Type-40 decode: live HR + R-R intervals** | ✅ **decoded** (NOOP layout, verified against our captured frames: 78 bpm + RR ms) — the dashboard shows it live |
| **Alarms + haptics** (`set_alarm_time` + `run_alarm`) | ✅ **physically felt on a wrist** — band reported `SUCCESSFUL_PLAY` |
| Battery pack info (`get_battery_pack_info`) | ✅ returns pack serial `WBB5AP0606717` |
| Historical storage cursors (`get_data_range`) | ✅ 65-byte body: ring boundary, read/write/ack cursors, capacity, estimates, 4 clock pairs (NOOP layout) |
| Bulk historical download (`send_historical_data` flow) | ⏳ scripted; ack now echoes the HISTORY_END token verbatim and only after disk persistence |
| Standard `0x2A37` heart rate | ⚠️ subscribes, stays silent on this band — live HR arrives via type-40 frames instead |
| **Tap detection** | ✅ confirmed — `DOUBLE_TAP` arrives as event frames (type 48, event id 14) |
| Optical debug reads (`40/42/44`), ext. battery (`98`), research (`132`) | ⛔ `UNSUPPORTED` on 5/MG — these are WHOOP-4-only commands (88 IDs are `U` on 50.42.1.0 and return result 3) |
| Body location (`84`) | 📖 needs `revision:u8=1` — response `[1,0,255,on_body]` (NOOP); our earlier bare-payload FAILURE explained |
| R18 biometric summary (HR, skin temp, steps, activity, motion state) | 📖 fully mapped by NOOP; not yet seen in our captures (historical pull pending) |
| SpO₂ | 📖 **not on the wire** for 5.0/MG (NOOP: raw red/IR scalars exist but are not percentages) |
| Sleep stages | 🕳️ still open: R18's motion state (WAKE/STILL/SLEEP/UP) is band state, **not** a hypnogram |

Operational quirks discovered live (ours) + documented (NOOP):

- **Alarm slots are 1–6** — slot 0 returns `INVALID_ALARM_ID`; waveform
  bytes must be ≤ 251; overall-repeat < 8 and counts pulses **after the
  first** (N−1); the record is 21 bytes with a **crescendo** byte at the
  end (our 20-byte payload worked because frame padding supplied the zero)
- `run_alarm` **clears the saved record** — it is not a nondestructive
  preview; disable is `[2, ID]` per slot or `[2, 255]` for all six
- **HISTORY_END ack (cmd 23)** must echo the 8-byte token from
  `frame[21:29]` verbatim, prefixed with `01` — and only after persisting,
  because the ack advances the band's trim boundary and may release
  stored device history
- Some commands (`63`, `11`, `98`) make the band **delay the ATT
  write-response ~10 s**. The Android app has a write watchdog that
  recovers; expect this when scripting.
- High-freq sync (96) revision 2: period must be strictly > 60,
  duration < 28 800 s
- The band streams battery-pack telemetry unprompted (puffin event frames
  carrying the pack serial); band clock is Unix-epoch synced

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

### Data packets — live & historical record layouts (NOOP, verified where noted)

**Packet 40 — live HR + R-R** (✅ verified against our captured frames):

| Offset | Width | Field |
|---:|---:|---|
| 10 | u32 | Unix-seconds timestamp |
| 14 | u16 | additional time field (scale unresolved) |
| 16 | u8 | **heart rate, bpm** |
| 17 | u8 | declared R-R count |
| 18+2i | u16 | R-R ticks, 1/1024 s → ms = `(ticks*1000+512)//1024` |

**Type 47/43 layout selector at byte 9** (Goose called it `packet_k`):
`18` R18 biometric summary, `20` R20 optical blocks, `21` R21 IMU,
`22` R22 queued channels (versioned 2–9), `26` R26 compact optical,
`16/17` ECG (MG); types 51/52 are dedicated IMU streams. For R18/R20/R21/R26:
record index u32 at 11, Unix seconds u32 at 15.

**R18 biometric summary** (124 bytes) — the biometric goldmine:

| Offset | Field |
|---:|---|
| 22 / 23, 24+2i | HR bpm / R-R count + words (1/1024 s) |
| 41, 45/49/53 | dynamic acceleration f32 + gravity x/y/z f32 (g) |
| 57 / 63 | step counter (hw; sw override at 61) / activity: 0 still, 1 walk, 2 run |
| 69, 71 / 73 | auxiliary thermal channels (raw/10 °C) / **skin temperature (raw/100 °C)** |
| 81 bits 4–5 | motion/rest state: **0 WAKE, 1 STILL, 2 SLEEP, 3 UP** (band state, not a hypnogram) |
| 106–109 | optical baseline/amplitude-like raw pairs (quality sentinels, not SpO₂) |

**Data range (cmd 34) final body** — 65 bytes: revision 1 + 16×u32: ring
boundary A, read cursor B, write cursor C, ack/trim boundary D, wrap state,
capacity, record-count estimate, derived `15×(adjD + cap − adjC)`, and four
clock pairs (boundary/trim/read/last-written pages).

### Command catalog

Frames are `[35, seq, cmd, payload...]`; names match `whoop_ble.py send`
(run `commands` for the live 70-entry list with payloads). The catalog
now covers the full documented 5/MG set (NOOP matrix: 71 supported /
88 unsupported on 50.42.1.0 — unsupported IDs return result 3).
Deliberately **excluded** from the catalog: firmware image transfer
(83, 142–144) and certificate/authorization commands (155–159,
incl. `LOCK_DEVICE`) — no update path is implemented here.

Highlights and corrections from the NOOP sync:

- **84** `get_body_location_and_status`: payload `01` (revision byte) →
  response `[1,0,255,on_body]` — fixes our bare-payload FAILURE
- **23** `historical_data_result`: **no fixed payload** — must echo the
  8-byte HISTORY_END token from `frame[21:29]` with prefix `01`, only
  after local persistence (⚠ the ack may release device history)
- **66** alarm record is 21 bytes (adds a crescendo byte); alarm IDs 1–6;
  validation detail codes match the haptics table (2=effect, 3=loops,
  4=duration, 10=time, 11=id)
- **68** RUN clears the saved record; pending `[2,0]` → success `[2,5]`
- **19** `run_haptic_pattern`: ad-hoc buzz without an alarm slot —
  `[01, 8 effects, u16 loop, u8 overall]`; **122** `stop_haptics` stops it
- **146/147** are the current clock SET/GET (10/11 deprecated)
- **115/116 & 117/118**: enumerate the 8 device-config keys and 25 feature
  flags by name — the previously-unknown 32-byte keys are now documented
  (e.g. `sigproc_wear_detect`, `cont_collection_mode`, `enable_r22_packets`,
  `disable_pip_r26_packets`)
- **148** worn-state override, **149** LED accessibility, **140/141**
  custom advertising name, **26** battery (u32 %), **81/82** raw-data
  capture start/stop, **1** link check
- Sensitive, in-catalog but loud: **15** forget_bonds, **25** force_trim,
  **33** set_read_pointer, **29/32** reboot/power-cycle

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

- **[NOOP](https://github.com/ryanbr/noop)** (ryanbr/noop) — the active,
  production-grade successor ecosystem (iOS/Android/macOS companion app,
  r/NoopBand). Its versioned protocol reference (`docs/PROTOCOL_*.md`,
  50.42.1.0 baseline) is now the primary source for command matrices,
  sensor-record layouts, alarms, configuration namespaces and ECG —
  most of the "brought up to speed" knowledge here. License: **PolyForm
  Noncommercial** (protocol facts referenced and credited; no code copied).
- **[Goose](https://github.com/b-nnett/goose)** (b-nnett/goose, archived) —
  the original WHOOP 5.0 BLE reverse-engineering (Swift + Rust PoC) this
  repo started from; frame format, UUIDs, event names, the captured hello.
- **[OpenWHOOP](https://github.com/pywebguru/OpenWHOOP)** and
  `johnmiddleton12/my-whoop` — the WHOOP 4.0 lineage both build on.

## Upstream tracking (notifications, all wired up)

**Two upstreams now, both watched by the automated canary**
(`.github/workflows/upstream-watch.yml`, weekly Monday 12:00 UTC + manual
dispatch):

- **ryanbr/noop (ACTIVE)** — watched for **new releases** (the integration
  points); HEAD recorded for reference. Watch the repo itself for
  commit-level news; a new release triggers the canary issue.
- **b-nnett/goose (ARCHIVED)** — watched for revival (HEAD movement,
  archived-flag flip) and **forks with recent pushes** (successor
  candidates; 5 were active in the last 30 days at time of writing).

When either moves, the canary opens/comments an issue in *this* repo
labeled `upstream-watch` — **watch this repo** (GitHub → Watch → All
activity) to get pinged. Manual watch points: the NOOP releases page,
<https://github.com/b-nnett/goose/commits/main.atom> (RSS), and
<https://github.com/b-nnett/goose/forks?include=active>.
Integration checklist lives in the agent appendix below.

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

1. Find the constant upstream — **NOOP first** (active, versioned), then
   Goose (frozen but byte-exact):
   - **NOOP** `docs/PROTOCOL_*.md`: `PROTOCOL_COMMANDS.md` (canonical
     matrix, every ID 1–159 with S/U status), `PROTOCOL_SENSORS.md`
     (packet-40, R18/R20/R21/R22/R26 layouts), `PROTOCOL_ALARMS.md`,
     `PROTOCOL_CONFIGURATION.md` (named keys), `PROTOCOL_TRANSPORT.md`
     (range/battery/history contracts), `PROTOCOL_ECG.md`. Mind the
     **PolyForm Noncommercial license**: reference and credit the facts,
     do not copy code.
   - **Goose**: `GooseSwift/GooseBLEClient.swift` (command enums),
     `Rust/core/src/protocol.rs` (parsers, `strap_event_name`,
     `data_packet_domain`), `GooseBLEClient+Parsing.swift`
     (`commandResultName`, `hapticsAlarmStatusName`,
     `historicalDataResultPayload` — the byte-exact ack-echo offsets).
2. Add to `COMMAND_CATALOG` in `whoop_ble.py`
   (`"name": (number, default_payload_hex_or_None, "description")`).
3. Update the README status/catalog sections with the new knowledge and
   its source (ours-live-verified vs documented).
4. New packet type → `TYPE_NAMES` (dashboard.py) + README packet table;
   new record layout → dashboard `enrich_entry` decoder + README tables.
5. Verify offline (`commands`, `send <name> --dry-run`), then the hello
   regression: `python.exe whoop_ble.py frame 1 145 01` →
   `aa0108000001e67123019101363e5c8d`.
6. Live-test via the phone bridge if available; record observed type-36
   responses in the README response tables, and update the status table
   at the top.
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
- Reads (`get_*`), `link_valid` and the hello are safe.
- `toggle_*` commands change what the band is streaming — confirm first,
  and always know the matching OFF payload.
- **`historical_data_result` is the most dangerous command in the
  catalog**: the payload must echo the HISTORY_END token verbatim
  (`01` + `frame[21:29]`), sent only after durable local persistence —
  it advances the band's trim boundary and can release stored history.
  Never synthesize or reuse a token.
- `run_alarm` clears the saved alarm record; `disable_alarm` `[2,255]`
  clears all six slots — never without explicit consent.
- **Never send**: `forget_bonds`, `force_trim`, `set_read_pointer`,
  power/reboot commands, or anything from the excluded image-transfer /
  certificate families (83, 142–144, 155–159) without explicit,
  informed user consent for that exact command.
- Do not remove or alter the disclaimers (not affiliated / at your own
  risk).
