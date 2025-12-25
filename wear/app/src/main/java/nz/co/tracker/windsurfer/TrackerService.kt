package nz.co.tracker.windsurfer

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Location
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.BatteryManager
import android.os.Binder
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.telephony.TelephonyManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LifecycleService
import com.google.android.gms.location.*
import kotlinx.coroutines.*
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

class TrackerService : LifecycleService() {

    companion object {
        private const val TAG = "TrackerService"
        private const val NOTIFICATION_ID = 1
        private const val CHANNEL_ID = "tracker_channel"

        const val DEFAULT_SERVER_HOST = "wstracker.org"
        const val DEFAULT_SERVER_PORT = 41234
        const val LOCATION_INTERVAL_MS = 10000L  // 10 seconds
        const val UDP_RETRY_COUNT = 3
        const val UDP_RETRY_DELAY_MS = 1500L
        const val ACK_TIMEOUT_MS = 2000L
        const val MAX_ACCURACY_METERS = 100.0f
    }

    // Binder for activity communication
    private val binder = LocalBinder()

    inner class LocalBinder : Binder() {
        fun getService(): TrackerService = this@TrackerService
    }

    // Location
    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private lateinit var locationCallback: LocationCallback
    private var lastLocation: Location? = null
    private var previousLocation: Location? = null

    // UDP
    private var socket: DatagramSocket? = null
    private var serverHost: String = DEFAULT_SERVER_HOST
    private var serverPort: Int = DEFAULT_SERVER_PORT
    private var sailorId: String = ""
    private var role: String = "sailor"
    private var password: String = ""
    private var eventId: Int = 2  // Event ID for multi-event support
    private var highFrequencyMode: Boolean = false
    private var heartRateEnabled: Boolean = false

    // 1Hz mode position buffer: [[ts, lat, lon, spd], ...]
    private data class BufferedPosition(val ts: Long, val lat: Double, val lon: Double, val spd: Double)
    private val positionBuffer = mutableListOf<BufferedPosition>()
    private var lastBufferedLocation: Location? = null

    // DNS caching
    private var cachedServerAddress: InetAddress? = null
    private var lastDnsLookupTime: Long = 0
    private val DNS_REFRESH_INTERVAL_MS = 300000L

    // State
    private val isRunning = AtomicBoolean(false)
    private val assistRequested = AtomicBoolean(false)
    private val sequenceNumber = AtomicInteger(0)
    private val lastAckTime = AtomicLong(0)
    // Sliding window for ACK rate calculation (last 20 messages)
    private val ackWindow = java.util.concurrent.ConcurrentLinkedDeque<Boolean>()
    private val ACK_WINDOW_SIZE = 20
    // Track sequences that have been recorded in the window (to avoid double-counting)
    private val recordedSeqs = java.util.concurrent.ConcurrentHashMap.newKeySet<Int>()

    // Track acknowledged sequence numbers to stop retransmissions
    private val acknowledgedSeqs = java.util.concurrent.ConcurrentHashMap.newKeySet<Int>()

    // Wake lock to keep tracking alive during battery saver
    private var wakeLock: PowerManager.WakeLock? = null

    // Network binding for standalone LTE/WiFi (bypasses Bluetooth proxy to phone)
    private var connectivityManager: ConnectivityManager? = null
    private var boundNetwork: Network? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    // Heart rate sensor
    private var sensorManager: SensorManager? = null
    private var heartRateSensor: Sensor? = null
    private var lastHeartRate: Int = -1  // -1 means not available
    private val heartRateListener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            if (event.sensor.type == Sensor.TYPE_HEART_RATE) {
                val hr = event.values[0].toInt()
                if (hr > 0) {
                    lastHeartRate = hr
                    Log.d(TAG, "Heart rate: $hr bpm")
                }
            }
        }
        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
    }

    // Coroutines
    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    // Listener for UI updates
    var statusListener: StatusListener? = null

    interface StatusListener {
        fun onLocationUpdate(location: Location)
        fun onAckReceived(seq: Int)
        fun onPacketSent(seq: Int)
        fun onConnectionStatus(ackRate: Float)
        fun onEventName(name: String)
        fun onError(message: String)
    }

    private fun getServerAddress(): InetAddress? {
        val now = System.currentTimeMillis()
        val cached = cachedServerAddress

        if (cached == null || (now - lastDnsLookupTime) > DNS_REFRESH_INTERVAL_MS) {
            try {
                val resolved = InetAddress.getByName(serverHost)
                cachedServerAddress = resolved
                lastDnsLookupTime = now
                if (cached == null) {
                    Log.i(TAG, "DNS resolved $serverHost to ${resolved.hostAddress}")
                }
                return resolved
            } catch (e: Exception) {
                if (cached != null) {
                    Log.w(TAG, "DNS lookup failed, using cached ${cached.hostAddress}")
                    return cached
                } else {
                    Log.e(TAG, "DNS lookup failed with no cached address", e)
                    return null
                }
            }
        }
        return cached
    }

    override fun onCreate() {
        super.onCreate()
        Log.d(TAG, "Service created")
        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)
        createNotificationChannel()
        setupLocationCallback()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)

        intent?.let {
            serverHost = it.getStringExtra("server_host") ?: DEFAULT_SERVER_HOST
            serverPort = it.getIntExtra("server_port", DEFAULT_SERVER_PORT)
            sailorId = it.getStringExtra("sailor_id") ?: ""
            role = it.getStringExtra("role") ?: "sailor"
            password = it.getStringExtra("password") ?: ""
            eventId = it.getIntExtra("event_id", 2)
            highFrequencyMode = it.getBooleanExtra("high_frequency_mode", false)
            heartRateEnabled = it.getBooleanExtra("heart_rate_enabled", false)
            positionBuffer.clear()
        }

        startForegroundService()
        startTracking()

        return START_STICKY
    }

    override fun onBind(intent: Intent): IBinder {
        super.onBind(intent)
        return binder
    }

    override fun onDestroy() {
        super.onDestroy()
        stopTracking()
        serviceScope.cancel()
        Log.d(TAG, "Service destroyed")
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Tracker Service",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Shows when tracking is active"
        }

        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(channel)
    }

    private fun startForegroundService() {
        val notification = buildNotification("Starting tracker...")
        startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
    }

    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, nz.co.tracker.windsurfer.presentation.MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Windsurfer Tracker")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val notification = buildNotification(text)
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, notification)
    }

    private fun setupLocationCallback() {
        locationCallback = object : LocationCallback() {
            override fun onLocationResult(result: LocationResult) {
                result.lastLocation?.let { location ->
                    if (MAX_ACCURACY_METERS > 0 && location.hasAccuracy() &&
                        location.accuracy > MAX_ACCURACY_METERS) {
                        Log.d(TAG, "Skipping inaccurate location: accuracy=${location.accuracy}m")
                        return
                    }

                    lastLocation = location
                    statusListener?.onLocationUpdate(location)

                    if (highFrequencyMode) {
                        // Buffer position for batched sending
                        val ts = System.currentTimeMillis() / 1000
                        val speedKnots = if (location.hasSpeed() && location.speed > 0) {
                            (location.speed * 1.94384 * 10).toInt() / 10.0  // Round to 1 decimal
                        } else 0.0
                        positionBuffer.add(BufferedPosition(ts, location.latitude, location.longitude, speedKnots))
                        lastBufferedLocation = location

                        // Send every 10 positions (10 seconds at 1Hz)
                        if (positionBuffer.size >= 10) {
                            sendPositionArray()
                        }
                    } else {
                        sendPosition(location)
                    }
                }
            }
        }
    }

    /**
     * Request a standalone network (LTE or WiFi) and bind the socket to it.
     * This bypasses the default Bluetooth proxy routing through the paired phone.
     */
    private fun requestStandaloneNetwork() {
        connectivityManager = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

        // Request a network with internet capability
        // NOT_VPN ensures we don't route through VPN
        // We prefer cellular or WiFi over Bluetooth proxy
        val networkRequest = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .addCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
            .build()

        networkCallback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                Log.i(TAG, "Network available: $network")
                boundNetwork = network

                // Bind the existing socket to this network
                socket?.let { sock ->
                    try {
                        network.bindSocket(sock)
                        Log.i(TAG, "Socket bound to network $network")

                        // Log network type for debugging
                        val caps = connectivityManager?.getNetworkCapabilities(network)
                        val networkType = when {
                            caps?.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) == true -> "CELLULAR"
                            caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true -> "WIFI"
                            caps?.hasTransport(NetworkCapabilities.TRANSPORT_BLUETOOTH) == true -> "BLUETOOTH"
                            else -> "OTHER"
                        }
                        Log.i(TAG, "Network type: $networkType")
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to bind socket to network", e)
                    }
                }
            }

            override fun onLost(network: Network) {
                Log.w(TAG, "Network lost: $network")
                if (boundNetwork == network) {
                    boundNetwork = null
                }
            }

            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                val networkType = when {
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "CELLULAR"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "WIFI"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_BLUETOOTH) -> "BLUETOOTH"
                    else -> "OTHER"
                }
                Log.d(TAG, "Network capabilities changed for $network: $networkType")
            }
        }

        try {
            connectivityManager?.requestNetwork(networkRequest, networkCallback!!)
            Log.i(TAG, "Requested standalone network")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to request network", e)
        }
    }

    /**
     * Release the network request
     */
    private fun releaseNetworkRequest() {
        networkCallback?.let { callback ->
            try {
                connectivityManager?.unregisterNetworkCallback(callback)
                Log.d(TAG, "Network callback unregistered")
            } catch (e: Exception) {
                Log.w(TAG, "Failed to unregister network callback", e)
            }
        }
        networkCallback = null
        boundNetwork = null
        connectivityManager = null
    }

    @Suppress("MissingPermission")
    private fun startTracking() {
        if (isRunning.getAndSet(true)) {
            Log.d(TAG, "Already tracking")
            return
        }

        // Clear acknowledged sequences from previous session
        acknowledgedSeqs.clear()

        // Acquire wake lock to keep tracking alive during battery saver
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "WindsurferTracker::TrackingWakeLock"
        ).apply {
            acquire()
        }
        Log.d(TAG, "Wake lock acquired")

        // Start heart rate sensor if available and enabled
        if (heartRateEnabled) {
            sensorManager = getSystemService(Context.SENSOR_SERVICE) as SensorManager
            heartRateSensor = sensorManager?.getDefaultSensor(Sensor.TYPE_HEART_RATE)
            if (heartRateSensor != null) {
                sensorManager?.registerListener(heartRateListener, heartRateSensor, SensorManager.SENSOR_DELAY_NORMAL)
                Log.d(TAG, "Heart rate sensor registered")
            } else {
                Log.d(TAG, "Heart rate sensor not available")
            }
        } else {
            Log.d(TAG, "Heart rate disabled by user preference")
        }

        Log.d(TAG, "Starting tracking to $serverHost:$serverPort as $sailorId (1Hz mode: $highFrequencyMode)")

        // Request standalone network (LTE/WiFi) before creating socket
        requestStandaloneNetwork()

        serviceScope.launch {
            try {
                socket = DatagramSocket()
                socket?.soTimeout = ACK_TIMEOUT_MS.toInt()

                // Bind socket to standalone network if already available
                boundNetwork?.let { network ->
                    try {
                        network.bindSocket(socket!!)
                        Log.i(TAG, "Socket bound to pre-existing network $network")
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to bind socket to network", e)
                    }
                }

                startAckListener()
            } catch (e: Exception) {
                Log.e(TAG, "Failed to create socket", e)
            }
        }

        // Use 1 second interval for 1Hz mode, 10 seconds otherwise
        val intervalMs = if (highFrequencyMode) 1000L else LOCATION_INTERVAL_MS
        val locationRequest = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, intervalMs)
            .setMinUpdateIntervalMillis(intervalMs / 2)
            .build()

        try {
            fusedLocationClient.requestLocationUpdates(
                locationRequest,
                locationCallback,
                Looper.getMainLooper()
            )
            updateNotification("Tracking active")
        } catch (e: SecurityException) {
            Log.e(TAG, "Location permission denied", e)
        }
    }

    private fun stopTracking() {
        if (!isRunning.getAndSet(false)) return

        Log.d(TAG, "Stopping tracking")
        fusedLocationClient.removeLocationUpdates(locationCallback)

        // Release network request before closing socket
        releaseNetworkRequest()

        socket?.close()
        socket = null

        // Unregister heart rate sensor
        sensorManager?.unregisterListener(heartRateListener)
        lastHeartRate = -1

        // Release wake lock
        wakeLock?.let {
            if (it.isHeld) {
                it.release()
                Log.d(TAG, "Wake lock released")
            }
        }
        wakeLock = null
    }

    private fun calculateDistance(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val earthRadius = 6371000.0
        val dLat = Math.toRadians(lat2 - lat1)
        val dLon = Math.toRadians(lon2 - lon1)
        val a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                Math.cos(Math.toRadians(lat1)) * Math.cos(Math.toRadians(lat2)) *
                Math.sin(dLon / 2) * Math.sin(dLon / 2)
        val c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a))
        return earthRadius * c
    }

    private fun calculateBearing(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val dLon = Math.toRadians(lon2 - lon1)
        val lat1Rad = Math.toRadians(lat1)
        val lat2Rad = Math.toRadians(lat2)
        val y = Math.sin(dLon) * Math.cos(lat2Rad)
        val x = Math.cos(lat1Rad) * Math.sin(lat2Rad) -
                Math.sin(lat1Rad) * Math.cos(lat2Rad) * Math.cos(dLon)
        var bearing = Math.toDegrees(Math.atan2(y, x))
        if (bearing < 0) bearing += 360.0
        return bearing
    }

    private fun sendPosition(location: Location) {
        val seq = sequenceNumber.incrementAndGet()

        val batteryManager = getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val batteryPercent = batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)

        // Get power/battery saver status
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        val isPowerSaveMode = powerManager.isPowerSaveMode
        // Note: isIgnoringBatteryOptimizations() is unreliable on Wear OS - don't report it

        val signalLevel = try {
            val telephonyManager = getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
            telephonyManager.signalStrength?.level ?: -1
        } catch (e: Exception) {
            -1
        }

        var speedMs = if (location.hasSpeed() && location.speed > 0) {
            location.speed.toDouble()
        } else {
            previousLocation?.let { prev ->
                val timeDelta = (location.time - prev.time) / 1000.0
                if (timeDelta > 0 && timeDelta < 300) {
                    val distance = calculateDistance(prev.latitude, prev.longitude,
                        location.latitude, location.longitude)
                    distance / timeDelta
                } else null
            } ?: 0.0
        }

        var bearing = if (location.hasBearing() && location.bearing != 0f) {
            location.bearing.toDouble()
        } else {
            previousLocation?.let { prev ->
                val distance = calculateDistance(prev.latitude, prev.longitude,
                    location.latitude, location.longitude)
                if (distance > 5) {
                    calculateBearing(prev.latitude, prev.longitude,
                        location.latitude, location.longitude)
                } else null
            } ?: 0.0
        }

        previousLocation = location

        // Build flags object (only power save mode - battery optimization unreliable on Wear OS)
        val flags = JSONObject().apply {
            put("ps", isPowerSaveMode)  // Power save mode (system battery saver)
        }

        val packet = JSONObject().apply {
            put("id", sailorId)
            put("eid", eventId)
            put("sq", seq)
            put("ts", System.currentTimeMillis() / 1000)
            put("lat", location.latitude)
            put("lon", location.longitude)
            if (location.hasAccuracy()) {
                put("hac", String.format("%.2f", location.accuracy).toDouble())  // Horizontal accuracy in meters
            }
            put("spd", String.format("%.2f", speedMs * 1.94384).toDouble())  // m/s to knots
            put("hdg", bearing.toInt())
            put("ast", assistRequested.get())
            put("bat", batteryPercent)
            put("sig", signalLevel)
            put("role", role)
            put("ver", BuildConfig.VERSION_STRING)
            put("os", "WearOS ${android.os.Build.VERSION.RELEASE}")
            put("flg", flags)
            if (lastHeartRate > 0) {
                put("hr", lastHeartRate)
            }
            if (password.isNotEmpty()) {
                put("pwd", password)
            }
        }

        val data = packet.toString().toByteArray(Charsets.UTF_8)

        serviceScope.launch {
            val address = getServerAddress()
            if (address == null) {
                Log.e(TAG, "Cannot send packet - no server address available")
                return@launch
            }

            repeat(UDP_RETRY_COUNT) { attempt ->
                // Stop retrying if we already got an ACK for this sequence
                if (acknowledgedSeqs.contains(seq)) {
                    Log.d(TAG, "Stopping retries for seq=$seq - already acknowledged")
                    return@launch
                }

                try {
                    val dgram = DatagramPacket(data, data.size, address, serverPort)
                    socket?.send(dgram)

                    if (attempt == 0) {
                        statusListener?.onPacketSent(seq)
                    }

                    Log.d(TAG, "Sent packet seq=$seq attempt=${attempt + 1}")

                    if (attempt < UDP_RETRY_COUNT - 1) {
                        delay(UDP_RETRY_DELAY_MS)
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to send packet", e)
                }
            }

            // If we exhausted all retries without ACK, record as failure
            if (!acknowledgedSeqs.contains(seq)) {
                recordSendResult(seq, false)
                statusListener?.onConnectionStatus(getAckRate())
            }
        }
    }

    /**
     * Send buffered positions as an array (1Hz mode)
     */
    private fun sendPositionArray() {
        if (positionBuffer.isEmpty()) return

        val location = lastBufferedLocation ?: return

        val seq = sequenceNumber.incrementAndGet()

        val batteryManager = getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val batteryPercent = batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)

        // Get power/battery saver status
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        val isPowerSaveMode = powerManager.isPowerSaveMode
        // Note: isIgnoringBatteryOptimizations() is unreliable on Wear OS - don't report it

        val signalLevel = try {
            val telephonyManager = getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
            telephonyManager.signalStrength?.level ?: -1
        } catch (e: Exception) {
            -1
        }

        var speedMs = if (location.hasSpeed() && location.speed > 0) {
            location.speed.toDouble()
        } else {
            previousLocation?.let { prev ->
                val timeDelta = (location.time - prev.time) / 1000.0
                if (timeDelta > 0 && timeDelta < 300) {
                    val distance = calculateDistance(prev.latitude, prev.longitude,
                        location.latitude, location.longitude)
                    distance / timeDelta
                } else null
            } ?: 0.0
        }

        var bearing = if (location.hasBearing() && location.bearing != 0f) {
            location.bearing.toDouble()
        } else {
            previousLocation?.let { prev ->
                val distance = calculateDistance(prev.latitude, prev.longitude,
                    location.latitude, location.longitude)
                if (distance > 5) {
                    calculateBearing(prev.latitude, prev.longitude,
                        location.latitude, location.longitude)
                } else null
            } ?: 0.0
        }

        previousLocation = location

        // Build position array: [[ts, lat, lon, spd], ...]
        val posArray = org.json.JSONArray()
        for (pos in positionBuffer) {
            val posEntry = org.json.JSONArray()
            posEntry.put(pos.ts)
            posEntry.put(pos.lat)
            posEntry.put(pos.lon)
            posEntry.put(pos.spd)
            posArray.put(posEntry)
        }

        val numPositions = positionBuffer.size
        positionBuffer.clear()

        // Build flags object (only power save mode - battery optimization unreliable on Wear OS)
        val flags = JSONObject().apply {
            put("ps", isPowerSaveMode)  // Power save mode (system battery saver)
        }

        val packet = JSONObject().apply {
            put("id", sailorId)
            put("eid", eventId)
            put("sq", seq)
            put("ts", System.currentTimeMillis() / 1000)
            put("pos", posArray)  // Position array instead of lat/lon
            if (location.hasAccuracy()) {
                put("hac", String.format("%.2f", location.accuracy).toDouble())  // Horizontal accuracy in meters
            }
            put("spd", String.format("%.2f", speedMs * 1.94384).toDouble())  // m/s to knots
            put("hdg", bearing.toInt())
            put("ast", assistRequested.get())
            put("bat", batteryPercent)
            put("sig", signalLevel)
            put("role", role)
            put("ver", BuildConfig.VERSION_STRING)
            put("os", "WearOS ${android.os.Build.VERSION.RELEASE}")
            put("flg", flags)
            if (lastHeartRate > 0) {
                put("hr", lastHeartRate)
            }
            if (password.isNotEmpty()) {
                put("pwd", password)
            }
        }

        val data = packet.toString().toByteArray(Charsets.UTF_8)

        serviceScope.launch {
            val address = getServerAddress()
            if (address == null) {
                Log.e(TAG, "Cannot send packet - no server address available")
                return@launch
            }

            repeat(UDP_RETRY_COUNT) { attempt ->
                // Stop retrying if we already got an ACK for this sequence
                if (acknowledgedSeqs.contains(seq)) {
                    Log.d(TAG, "Stopping retries for seq=$seq - already acknowledged")
                    return@launch
                }

                try {
                    val dgram = DatagramPacket(data, data.size, address, serverPort)
                    socket?.send(dgram)

                    if (attempt == 0) {
                        statusListener?.onPacketSent(seq)
                    }

                    Log.d(TAG, "Sent array packet seq=$seq with $numPositions positions, attempt=${attempt + 1}")

                    if (attempt < UDP_RETRY_COUNT - 1) {
                        delay(UDP_RETRY_DELAY_MS)
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to send packet", e)
                }
            }

            // If we exhausted all retries without ACK, record as failure
            if (!acknowledgedSeqs.contains(seq)) {
                recordSendResult(seq, false)
                statusListener?.onConnectionStatus(getAckRate())
            }
        }
    }

    private fun startAckListener() {
        serviceScope.launch {
            val buffer = ByteArray(256)

            while (isRunning.get()) {
                try {
                    val dgram = DatagramPacket(buffer, buffer.size)
                    socket?.receive(dgram)

                    val response = String(dgram.data, 0, dgram.length, Charsets.UTF_8)
                    val ack = JSONObject(response)
                    val ackSeq = ack.optInt("ack", -1)

                    if (ackSeq > 0) {
                        // Check for error in response
                        val errorType = ack.optString("error", "")
                        val errorMsg = ack.optString("msg", "")
                        if (errorType.isNotEmpty()) {
                            Log.e(TAG, "Server error: $errorType - $errorMsg")
                            statusListener?.onError(errorMsg.ifEmpty { "Server error: $errorType" })
                            // Don't count as successful ACK
                            continue
                        }

                        // Mark this sequence as acknowledged to stop retransmissions
                        acknowledgedSeqs.add(ackSeq)

                        // Clean up old sequence numbers (keep only recent ones)
                        val currentSeq = sequenceNumber.get()
                        acknowledgedSeqs.removeIf { it < currentSeq - 100 }

                        lastAckTime.set(System.currentTimeMillis())

                        // Record success in sliding window
                        recordSendResult(ackSeq, true)

                        val ackRate = getAckRate()
                        statusListener?.onAckReceived(ackSeq)
                        statusListener?.onConnectionStatus(ackRate)

                        // Extract event name from ACK if present
                        val eventName = ack.optString("event", "")
                        if (eventName.isNotEmpty()) {
                            statusListener?.onEventName(eventName)
                        }

                        Log.d(TAG, "Received ACK for seq=$ackSeq${if (eventName.isNotEmpty()) " (event: $eventName)" else ""}")
                    }
                } catch (e: java.net.SocketTimeoutException) {
                    // Normal timeout, continue
                } catch (e: Exception) {
                    if (isRunning.get()) {
                        Log.e(TAG, "ACK listener error", e)
                    }
                }
            }
        }
    }

    // Public methods for UI
    fun getLastLocation(): Location? = lastLocation

    fun getAckRate(): Float {
        val window = ackWindow.toList()
        if (window.isEmpty()) return 0f
        return window.count { it }.toFloat() / window.size
    }

    /**
     * Record a send result in the sliding window.
     * @param seq The sequence number
     * @param success True if ACK was received, false if timed out
     */
    private fun recordSendResult(seq: Int, success: Boolean) {
        // Only record each sequence once
        if (!recordedSeqs.add(seq)) return

        // Add to window
        ackWindow.addLast(success)

        // Trim window to size
        while (ackWindow.size > ACK_WINDOW_SIZE) {
            ackWindow.removeFirst()
        }

        // Clean up old recorded sequences
        val currentSeq = sequenceNumber.get()
        recordedSeqs.removeIf { it < currentSeq - 100 }
    }

    fun getLastAckTime(): Long = lastAckTime.get()

    fun isTracking(): Boolean = isRunning.get()

    fun isAssistActive(): Boolean = assistRequested.get()

    fun requestAssist(enabled: Boolean) {
        assistRequested.set(enabled)
        Log.d(TAG, "Assist ${if (enabled) "ENABLED" else "disabled"}")

        // Send immediate position update when requesting assist
        if (enabled) {
            lastLocation?.let { sendPosition(it) }
        }
    }

    fun stopService() {
        stopTracking()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }
}
