package com.whoopble.bridge

import android.Manifest
import android.app.Activity
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.WindowInsets
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * WHOOP Bridge: turns this phone into a BLE radio proxy for the PC.
 *
 * Connects to a WHOOP 5.0 band (existing OS-level bond provides the
 * encryption for the locked custom service), subscribes to every WHOOP
 * notification characteristic plus the standard heart-rate characteristic,
 * and exposes the same HTTP control surface as bridge_server.py on the PC:
 *
 *   GET  /state              {connected, band, log_last_id}
 *   GET  /log?since=N        events after id N
 *   GET  /help                endpoint list
 *   POST /write {"frame":hex} queue a raw frame for the command characteristic
 *   POST /cmd/frame/<hex>     same, path form (parity with bridge_server.py)
 *   POST /reset               drop queued writes
 *
 * The HTTP server binds 127.0.0.1 only - control it from the PC via
 *   adb forward tcp:8443 tcp:8443
 * then curl http://localhost:8443/... on the PC.
 *
 * Zero external dependencies. All frame building / decoding stays on the PC
 * (whoop_ble.py); this app is a deliberately dumb modem.
 */
class MainActivity : Activity() {

    companion object {
        val V5_SERVICE: UUID = UUID.fromString("fd4b0001-cce1-4033-93ce-002d5875f58a")
        val GEN4_SERVICE: UUID = UUID.fromString("61080001-8d6d-82b8-614a-1c8cb0f8dcc6")
        val V5_CMD: UUID = UUID.fromString("fd4b0002-cce1-4033-93ce-002d5875f58a")
        val GEN4_CMD: UUID = UUID.fromString("61080002-8d6d-82b8-614a-1c8cb0f8dcc6")
        val NOTIFY_CHARS: List<UUID> = listOf(
            "fd4b0003-cce1-4033-93ce-002d5875f58a",
            "fd4b0004-cce1-4033-93ce-002d5875f58a",
            "fd4b0005-cce1-4033-93ce-002d5875f58a",
            "fd4b0007-cce1-4033-93ce-002d5875f58a",
            "61080003-8d6d-82b8-614a-1c8cb0f8dcc6",
            "61080004-8d6d-82b8-614a-1c8cb0f8dcc6",
            "61080005-8d6d-82b8-614a-1c8cb0f8dcc6",
            "61080007-8d6d-82b8-614a-1c8cb0f8dcc6",
        ).map { UUID.fromString(it) }
        val HR_SERVICE: UUID = UUID.fromString("0000180d-0000-1000-8000-00805f9b34fb")
        val HR_MEAS: UUID = UUID.fromString("00002a37-0000-1000-8000-00805f9b34fb")
        val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        const val PORT = 8443
        const val HELLO_FRAME = "aa0108000001e67123019101363e5c8d"
        const val SCAN_TIMEOUT_MS = 25_000L

        // ---- process-wide state (survives activity recreation) --------------
        // onCreate can run again (back button → am start) while the process
        // lives; the HTTP server + BLE state must NOT be tied to the Activity.
        val uiHandler = Handler(Looper.getMainLooper())
        val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)
        val lock = ReentrantLock()
        val events = ArrayList<JSONObject>()
        var eventSeq = 0
        val writeQueue = ArrayDeque<String>()
        @Volatile var writeInFlight = false
        @Volatile var writeStartedAt = 0L
        val cccdQueue = ArrayDeque<BluetoothGattCharacteristic>()
        @Volatile var ready = false
        @Volatile var httpStarted = false
        var gattRef: BluetoothGatt? = null
        var cmdCharRef: BluetoothGattCharacteristic? = null
        @Volatile var bandName: String? = null
        @Volatile var connectedFlag = false
        @Volatile var tvRef: TextView? = null
    }

    // ---- logging / event bus ---------------------------------------------
    private fun logEvent(kind: String, char: String, hex: String, desc: String = "") {
        val entry = JSONObject().apply {
            put("ts", timeFmt.format(Date()))
            put("kind", kind)
            put("char", char)
            put("hex", hex)
            put("desc", desc)
        }
        lock.withLock {
            eventSeq++
            entry.put("id", eventSeq)
            events.add(entry)
            if (events.size > 2000) events.removeAt(0)
        }
        uiHandler.post {
            tvRef?.let { t ->
                t.text = "${entry.getString("ts")}  [$kind] ${if (desc.isEmpty()) "" else "($desc) "} $hex\n${t.text}"
            }
        }
    }

    private fun stateJson(): JSONObject = lock.withLock {
        JSONObject().apply {
            put("connected", connectedFlag)
            put("ready", ready)
            put("band", bandName ?: JSONObject.NULL)
            put("queued", writeQueue.size)
            put("log_last_id", eventSeq)
        }
    }

    private fun logSince(since: Int): JSONArray = lock.withLock {
        val arr = JSONArray()
        events.dropWhile { it.getInt("id") <= since }.forEach { arr.put(it) }
        arr
    }

    // ---- BLE ---------------------------------------------------------------
    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            when (newState) {
                BluetoothGatt.STATE_CONNECTED -> {
                    logEvent("gatt", "", "", "connected, requesting MTU 247")
                    g.requestMtu(247)
                }
                BluetoothGatt.STATE_DISCONNECTED -> {
                    lock.withLock {
                        connectedFlag = false; ready = false
                        writeQueue.clear(); writeInFlight = false; cccdQueue.clear()
                    }
                    logEvent("gatt", "", "", "disconnected (status=$status)")
                    uiHandler.post { g.close() }
                    gattRef = null; cmdCharRef = null
                }
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            logEvent("gatt", "", "", "MTU $mtu, discovering services")
            g.discoverServices()
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                logEvent("gatt", "", "", "service discovery failed: $status")
                return
            }
            var svc = g.getService(V5_SERVICE)
            var cmdId = V5_CMD
            if (svc == null) {
                svc = g.getService(GEN4_SERVICE)
                cmdId = GEN4_CMD
            }
            if (svc == null) {
                logEvent("gatt", "", "", "no WHOOP service found")
                return
            }
            cmdCharRef = svc.getCharacteristic(cmdId)
            logEvent("gatt", "", "", "found WHOOP service; cmd char=${cmdCharRef != null}")

            // queue CCCD (notification enable) writes, one at a time
            for (u in NOTIFY_CHARS) {
                val c = svc.getCharacteristic(u) ?: continue
                if (g.setCharacteristicNotification(c, true)) cccdQueue.add(c)
            }
            g.getService(HR_SERVICE)?.getCharacteristic(HR_MEAS)?.let { c ->
                if (g.setCharacteristicNotification(c, true)) cccdQueue.add(c)
            }
            drainCccd(g)
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            logEvent("cccd", d.characteristic.uuid.toString(), "",
                     if (status == 0) "subscribed" else "failed status=$status")
            drainCccd(g)
        }

        @Deprecated("Deprecated in Java")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            val bytes = c.value ?: return
            val hex = bytesToHex(bytes)
            logEvent("notify", c.uuid.toString(), hex, describe(c.uuid, bytes))
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            lock.withLock { writeInFlight = false }
            logEvent("write_result", c.uuid.toString(), bytesToHex(c.value ?: ByteArray(0)),
                     if (status == 0) "ok" else "failed status=$status")
            drainWrites(g, c)
        }
    }

    private fun drainCccd(g: BluetoothGatt) {
        val next = lock.withLock { if (cccdQueue.isEmpty()) null else cccdQueue.removeFirst() }
        if (next == null) {
            lock.withLock { ready = true }
            logEvent("gatt", "", "", "READY - all subscriptions done")
            return
        }
        val d = next.getDescriptor(CCCD)
        if (d == null) {
            logEvent("cccd", next.uuid.toString(), "", "no CCCD descriptor")
            drainCccd(g)
            return
        }
        d.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
        if (!g.writeDescriptor(d)) {
            logEvent("cccd", next.uuid.toString(), "", "writeDescriptor call failed")
            drainCccd(g)
        }
    }

    private fun drainWrites(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
        val hexframe = lock.withLock {
            if (writeInFlight || writeQueue.isEmpty()) return
            writeInFlight = true
            writeStartedAt = SystemClock.elapsedRealtime()
            writeQueue.removeFirst()
        }
        c.value = hexToBytes(hexframe)
        c.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
        val ok = g.writeCharacteristic(c)
        if (!ok) {
            lock.withLock { writeInFlight = false }
            logEvent("write_result", c.uuid.toString(), hexframe, "writeCharacteristic call failed")
        } else {
            logEvent("write_attempt", c.uuid.toString(), hexframe)
        }
    }

    private fun startWriteWatchdog() {
        uiHandler.postDelayed(object : Runnable {
            override fun run() {
                val stuck = lock.withLock {
                    writeInFlight && SystemClock.elapsedRealtime() - writeStartedAt > 8000
                }
                if (stuck) {
                    lock.withLock { writeInFlight = false; writeStartedAt = 0 }
                    logEvent("write_result", "", "", "watchdog: stuck write flag reset after 8s")
                    val g = gattRef; val c = cmdCharRef
                    if (g != null && c != null) drainWrites(g, c)
                }
                uiHandler.postDelayed(this, 3000)
            }
        }, 3000)
    }

    private fun enqueueWrite(hex: String) {
        lock.withLock { writeQueue.add(hex) }
        logEvent("queued", "", hex)
        val g = gattRef; val c = cmdCharRef
        if (g != null && c != null) drainWrites(g, c)
    }

    private fun describe(uuid: UUID, bytes: ByteArray): String {
        if (uuid == HR_MEAS && bytes.isNotEmpty()) {
            val flags = bytes[0].toInt()
            var i = 1
            val bpm = if (flags and 0x01 != 0) {
                val b = ((bytes[i].toInt() and 0xFF) or ((bytes[i + 1].toInt() and 0xFF) shl 8)); i += 2; b
            } else {
                (bytes[i].toInt() and 0xFF); i += 1
            }
            var rrCount = 0
            if (flags and 0x10 != 0) {
                while (i + 2 <= bytes.size) { rrCount++; i += 2 }
            }
            return if (rrCount > 0) "HR $bpm bpm (+$rrCount RR)" else "HR $bpm bpm"
        }
        // WHOOP frame peek: 0xAA start, packet type at offset 8
        if (bytes.size > 10 && bytes[0] == 0xAA.toByte()) {
            return "WHOOP frame type=${bytes[8].toInt() and 0xFF} seq=${bytes[9].toInt() and 0xFF}"
        }
        return ""
    }

    // ---- scan ---------------------------------------------------------------
    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val name = result.device.name ?: return
            if (!name.startsWith("WHOOP", ignoreCase = true)) return
            stopScan()
            logEvent("scan", "", "", "found $name ${result.device.address} rssi=${result.rssi}")
            connectDevice(result)
        }

        override fun onScanFailed(errorCode: Int) {
            logEvent("scan", "", "", "scan failed: $errorCode")
        }
    }

    private fun stopScan() {
        try {
            (getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager)
                .adapter.bluetoothLeScanner.stopScan(scanCallback)
        } catch (_: Exception) {}
    }

    private fun connectDevice(result: ScanResult) {
        uiHandler.post {
            bandName = result.device.name
            logEvent("gatt", "", "", "connecting to ${result.device.address}")
            gattRef = result.device.connectGatt(this, false, gattCallback,
                                                android.bluetooth.BluetoothDevice.TRANSPORT_LE)
            lock.withLock { connectedFlag = true }
        }
    }

    private fun startScan() {
        stopScan()
        logEvent("scan", "", "", "scanning for WHOOP (make sure the WHOOP app is not holding the band)")
        val scanner = (getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager)
            .adapter.bluetoothLeScanner
        scanner.startScan(null, ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), scanCallback)
        uiHandler.postDelayed({ stopScan(); logEvent("scan", "", "", "scan timeout") }, SCAN_TIMEOUT_MS)
    }

    // ---- HTTP control server (loopback only) --------------------------------
    private fun startHttpServer() {
        Thread {
            try {
                val server = ServerSocket(PORT, 16, InetAddress.getByName("127.0.0.1"))
                logEvent("http", "", "", "control server on 127.0.0.1:$PORT (use adb forward)")
                while (true) {
                    val client = server.accept()
                    Thread { handleClient(client) }.start()
                }
            } catch (e: Exception) {
                logEvent("http", "", "", "server error: $e")
            }
        }.apply { isDaemon = true }.start()
    }

    private fun handleClient(client: Socket) {
        client.use { s ->
            try {
                val input = BufferedReader(InputStreamReader(s.getInputStream(), Charsets.ISO_8859_1))
                val requestLine = input.readLine() ?: return
                val parts = requestLine.split(" ")
                if (parts.size < 2) return
                val method = parts[0]
                val rawPath = parts[1]
                var contentLength = 0
                while (true) {
                    val line = input.readLine() ?: break
                    if (line.isEmpty()) break
                    if (line.startsWith("Content-Length:", ignoreCase = true)) {
                        contentLength = line.substringAfter(':').trim().toInt()
                    }
                }
                var body = ""
                if (contentLength > 0) {
                    val buf = CharArray(contentLength)
                    var read = 0
                    while (read < contentLength) {
                        val r = input.read(buf, read, contentLength - read)
                        if (r < 0) break
                        read += r
                    }
                    body = String(buf, 0, read)
                }

                val path = rawPath.substringBefore('?')
                val query = rawPath.substringAfter('?', "")
                val response = route(method, path, query, body)
                val bytes = response.toString().toByteArray(Charsets.UTF_8)
                val out = s.getOutputStream()
                out.write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n".toByteArray(Charsets.ISO_8859_1))
                out.write(bytes)
                out.flush()
            } catch (_: Exception) {
                // client hung up; nothing to do
            }
        }
    }

    private fun route(method: String, path: String, query: String, body: String): JSONObject {
        val q = query.split('&').mapNotNull {
            val k = it.substringBefore('='); val v = it.substringAfter('=', "")
            if (k.isNotEmpty()) k to v else null
        }.toMap()

        if (method == "GET" && path == "/state") return stateJson()

        if (method == "GET" && path == "/log") {
            val since = q["since"]?.toIntOrNull() ?: 0
            return JSONObject().put("entries", logSince(since))
                          .put("last_log_id", lock.withLock { eventSeq })
        }

        if (method == "GET" && path == "/help") {
            return JSONObject().apply {
                put("get", JSONArray(listOf("/state", "/log?since=N", "/help")))
                put("post", JSONArray(listOf("/write", "/cmd/frame/<hex>", "/reset", "/cmd/hello")))
                put("note", "frames are built on the PC by whoop_ble.py; this app only writes raw hex")
            }
        }

        if (method == "POST" && path == "/write") {
            val hex = try { JSONObject(body).optString("frame", "") } catch (_: Exception) { "" }
            if (hex.isEmpty()) return JSONObject().put("error", "body needs {\"frame\":\"<hex>\"}")
            enqueueWrite(hex)
            return JSONObject().put("ok", true).put("queued", hex)
        }

        if (method == "POST" && path == "/cmd/hello") {
            enqueueWrite(HELLO_FRAME)
            return JSONObject().put("ok", true).put("queued", HELLO_FRAME)
        }

        if (method == "POST" && path.startsWith("/cmd/frame/")) {
            val hex = path.substringAfter("/cmd/frame/")
            if (hex.isEmpty() || hex.length % 2 != 0 || !hex.all { it.isDigit() || it in 'a'..'f' || it in 'A'..'F' }) {
                return JSONObject().put("error", "bad hex")
            }
            enqueueWrite(hex)
            return JSONObject().put("ok", true).put("queued", hex)
        }

        if (method == "POST" && path == "/reset") {
            val n = lock.withLock { val n = writeQueue.size; writeQueue.clear(); n }
            return JSONObject().put("ok", true).put("dropped", n)
        }

        return JSONObject().put("error", "not found").put("hint", "GET /help")
    }

    // ---- permissions + UI -----------------------------------------------------
    @Deprecated("Deprecated in Java")
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 1 && grantResults.isNotEmpty() && grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
            startScan()
        } else {
            logEvent("perms", "", "", "BLUETOOTH permissions denied")
        }
    }

    private fun connectClicked() {
        if (checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED &&
            checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED) {
            startScan()
        } else {
            @Suppress("DEPRECATION")
            requestPermissions(arrayOf(
                Manifest.permission.BLUETOOTH_SCAN,
                Manifest.permission.BLUETOOTH_CONNECT), 1)
        }
    }

    private fun disconnectClicked() {
        uiHandler.post {
            gattRef?.disconnect()
            logEvent("ui", "", "", "disconnect requested")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val pad = (resources.displayMetrics.density * 12).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        // Android 15+ (targetSdk 35) enforces edge-to-edge: content draws
        // behind system bars unless the app applies window insets itself.
        root.setOnApplyWindowInsetsListener { v, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars())
            v.setPadding(pad + bars.left, pad + bars.top, pad + bars.right, pad + bars.bottom)
            insets
        }
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val btnConnect = Button(this).apply {
            text = "CONNECT TO WHOOP"; setOnClickListener { connectClicked() }
        }
        val btnDisconnect = Button(this).apply {
            text = "DISCONNECT"; setOnClickListener { disconnectClicked() }
        }
        val btnClear = Button(this).apply {
            text = "CLEAR"; setOnClickListener { _ -> uiHandler.post { tvRef?.text = "" } }
        }
        buttons.addView(btnConnect)
        buttons.addView(btnDisconnect)
        buttons.addView(btnClear)
        root.addView(buttons)

        val scroll = ScrollView(this)
        val tv = TextView(this).apply {
            typeface = android.graphics.Typeface.MONOSPACE
            textSize = 11f
            text = "WHOOP Bridge ready. Control: adb forward tcp:8443 tcp:8443\n"
        }
        tvRef = tv
        scroll.addView(tv)
        root.addView(scroll, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.MATCH_PARENT))

        setContentView(root)
        lock.withLock {
            if (!httpStarted) {
                httpStarted = true
                startHttpServer()
                startWriteWatchdog()
            }
        }
    }
}

// ---- hex helpers -------------------------------------------------------------
internal fun bytesToHex(bytes: ByteArray): String =
    bytes.joinToString("") { String.format("%02x", it) }

internal fun hexToBytes(hex: String): ByteArray =
    hex.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
