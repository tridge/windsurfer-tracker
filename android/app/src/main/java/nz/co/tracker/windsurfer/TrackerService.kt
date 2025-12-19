package nz.co.tracker.windsurfer

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.location.Location
import android.os.BatteryManager
import android.os.Binder
import android.os.Build
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
        
        // Configuration - could be moved to preferences
        const val DEFAULT_SERVER_HOST = "wstracker.org"
        const val DEFAULT_SERVER_PORT = 41234
        const val LOCATION_INTERVAL_MS = 10000L  // 10 seconds
        const val UDP_RETRY_COUNT = 3
        const val UDP_RETRY_DELAY_MS = 1500L
        const val ACK_TIMEOUT_MS = 2000L
        // Accuracy filtering: reject locations with accuracy worse than this (meters)
        // 0 = disabled. OwnTracks uses similar filtering.
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
    private var previousLocation: Location? = null  // For calculating speed/bearing
    
    // UDP
    private var socket: DatagramSocket? = null
    private var serverHost: String = DEFAULT_SERVER_HOST
    private var serverPort: Int = DEFAULT_SERVER_PORT
    private var sailorId: String = ""
    private var role: String = "sailor"  // sailor, support, spectator
    // Note: password is read from SharedPreferences on each send to pick up changes immediately
    private var highFrequencyMode: Boolean = false  // 1Hz mode - send positions as array

    // 1Hz mode position buffer: [[ts, lat, lon], ...]
    private val positionBuffer = mutableListOf<Triple<Long, Double, Double>>()
    private var lastBufferedLocation: Location? = null

    // Battery drain tracking
    private var trackingStartTime: Long = 0
    private var trackingStartBattery: Int = -1

    // DNS caching - resolve once and cache to avoid failures on bad networks
    private var cachedServerAddress: InetAddress? = null
    private var lastDnsLookupTime: Long = 0
    private val DNS_REFRESH_INTERVAL_MS = 300000L  // Retry DNS every 5 minutes if we have a cached address

    /**
     * Get the server address, using cached DNS resolution to survive network issues.
     * Returns null only if DNS has never successfully resolved.
     */
    private fun getServerAddress(): InetAddress? {
        val now = System.currentTimeMillis()
        val cached = cachedServerAddress

        // If we have no cached address, or it's time to refresh, try DNS lookup
        if (cached == null || (now - lastDnsLookupTime) > DNS_REFRESH_INTERVAL_MS) {
            try {
                val resolved = InetAddress.getByName(serverHost)
                cachedServerAddress = resolved
                lastDnsLookupTime = now
                if (cached == null) {
                    Log.i(TAG, "DNS resolved $serverHost to ${resolved.hostAddress}")
                } else if (resolved.hostAddress != cached.hostAddress) {
                    Log.i(TAG, "DNS updated $serverHost: ${cached.hostAddress} -> ${resolved.hostAddress}")
                }
                return resolved
            } catch (e: Exception) {
                if (cached != null) {
                    // DNS failed but we have a cached address - use it
                    Log.w(TAG, "DNS lookup failed for $serverHost, using cached ${cached.hostAddress}")
                    return cached
                } else {
                    // No cached address and DNS failed - can't proceed
                    Log.e(TAG, "DNS lookup failed for $serverHost with no cached address", e)
                    return null
                }
            }
        }

        return cached
    }

    // State
    private val isRunning = AtomicBoolean(false)
    private val assistRequested = AtomicBoolean(false)
    private val sequenceNumber = AtomicInteger(0)
    private val lastAckTime = AtomicLong(0)
    private val packetsAcked = AtomicInteger(0)
    private val packetsSent = AtomicInteger(0)

    // Track acknowledged sequence numbers to stop retransmissions
    private val acknowledgedSeqs = java.util.concurrent.ConcurrentHashMap.newKeySet<Int>()
    
    // Coroutines
    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    
    // Listener for UI updates
    var statusListener: StatusListener? = null
    
    interface StatusListener {
        fun onLocationUpdate(location: Location)
        fun onAckReceived(seq: Int)
        fun onPacketSent(seq: Int)
        fun onConnectionStatus(ackRate: Float)
        fun onAuthError(message: String)
    }

    /**
     * Get the current password from SharedPreferences.
     * This is read on each send so settings changes take effect immediately.
     */
    private fun getCurrentPassword(): String {
        val prefs = getSharedPreferences("tracker_prefs", Context.MODE_PRIVATE)
        return prefs.getString("password", "") ?: ""
    }

    /**
     * Get the current event ID from SharedPreferences.
     * This is read on each send so settings changes take effect immediately.
     * Defaults to 1 for backwards compatibility.
     */
    private fun getCurrentEventId(): Int {
        val prefs = getSharedPreferences("tracker_prefs", Context.MODE_PRIVATE)
        return prefs.getInt("event_id", 1)
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
        
        // Extract configuration from intent
        intent?.let {
            serverHost = it.getStringExtra("server_host") ?: DEFAULT_SERVER_HOST
            serverPort = it.getIntExtra("server_port", DEFAULT_SERVER_PORT)
            sailorId = it.getStringExtra("sailor_id") ?: ""
            role = it.getStringExtra("role") ?: "sailor"
            // Password is read from SharedPreferences on each send (not cached)
            highFrequencyMode = it.getBooleanExtra("high_frequency_mode", false)
            // Clear position buffer when mode changes
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
        
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }
    
    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java)
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
                    // Filter out inaccurate locations (technique from OwnTracks)
                    if (MAX_ACCURACY_METERS > 0 && location.hasAccuracy() &&
                        location.accuracy > MAX_ACCURACY_METERS) {
                        Log.d(TAG, "Skipping inaccurate location: accuracy=${location.accuracy}m > ${MAX_ACCURACY_METERS}m")
                        return
                    }

                    lastLocation = location
                    statusListener?.onLocationUpdate(location)

                    if (highFrequencyMode) {
                        // Buffer position for batched sending
                        val ts = System.currentTimeMillis() / 1000
                        positionBuffer.add(Triple(ts, location.latitude, location.longitude))
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
    
    @Suppress("MissingPermission")  // Permission checked in MainActivity
    private fun startTracking() {
        if (isRunning.getAndSet(true)) {
            Log.d(TAG, "Already tracking")
            return
        }

        // Clear acknowledged sequences from previous session
        acknowledgedSeqs.clear()

        Log.d(TAG, "Starting tracking to $serverHost:$serverPort as $sailorId (1Hz mode: $highFrequencyMode)")

        // Record starting battery for drain rate calculation
        trackingStartTime = System.currentTimeMillis()
        try {
            val batteryManager = getSystemService(Context.BATTERY_SERVICE) as BatteryManager
            trackingStartBattery = batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
            Log.d(TAG, "Starting battery: $trackingStartBattery%")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to get starting battery", e)
            trackingStartBattery = -1
        }

        // Initialize socket
        serviceScope.launch {
            try {
                socket = DatagramSocket()
                socket?.soTimeout = ACK_TIMEOUT_MS.toInt()
                startAckListener()
            } catch (e: Exception) {
                Log.e(TAG, "Failed to create socket", e)
            }
        }

        // Start location updates - use 1 second interval for 1Hz mode
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
        socket?.close()
        socket = null
    }
    
    /**
     * Calculate distance between two points using Haversine formula
     * @return distance in meters
     */
    private fun calculateDistance(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val earthRadius = 6371000.0  // meters
        val dLat = Math.toRadians(lat2 - lat1)
        val dLon = Math.toRadians(lon2 - lon1)
        val a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                Math.cos(Math.toRadians(lat1)) * Math.cos(Math.toRadians(lat2)) *
                Math.sin(dLon / 2) * Math.sin(dLon / 2)
        val c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a))
        return earthRadius * c
    }

    /**
     * Calculate bearing from point 1 to point 2
     * @return bearing in degrees (0-360)
     */
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

        // Get battery level and charging state
        val batteryManager = getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val batteryPercent = batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val isCharging = batteryManager.isCharging

        // Calculate battery drain rate (%/hr) - need at least 5 minutes of tracking
        var drainRate: Double? = null
        if (trackingStartTime > 0 && trackingStartBattery >= 0 && batteryPercent >= 0) {
            val elapsedMs = System.currentTimeMillis() - trackingStartTime
            if (elapsedMs >= 5 * 60 * 1000) {  // 5 minutes minimum
                val drainPercent = trackingStartBattery - batteryPercent
                val hoursElapsed = elapsedMs / (1000.0 * 3600.0)
                if (hoursElapsed > 0) {
                    drainRate = drainPercent / hoursElapsed
                }
            }
        }

        // Get power/battery saver status
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        val isPowerSaveMode = powerManager.isPowerSaveMode
        val isBatteryOptIgnored = powerManager.isIgnoringBatteryOptimizations(packageName)

        // Get signal strength (0-4 bars, or -1 if unavailable)
        val signalLevel = try {
            val telephonyManager = getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
            telephonyManager.signalStrength?.level ?: -1
        } catch (e: Exception) {
            -1
        }

        // Get speed - use native if available, otherwise calculate from previous position
        var speedMs = if (location.hasSpeed() && location.speed > 0) {
            location.speed.toDouble()
        } else {
            // Calculate from previous location
            previousLocation?.let { prev ->
                val timeDelta = (location.time - prev.time) / 1000.0  // seconds
                if (timeDelta > 0 && timeDelta < 300) {  // Only if < 5 minutes gap
                    val distance = calculateDistance(prev.latitude, prev.longitude,
                                                     location.latitude, location.longitude)
                    distance / timeDelta
                } else null
            } ?: 0.0
        }

        // Get bearing - use native if available, otherwise calculate from previous position
        var bearing = if (location.hasBearing() && location.bearing != 0f) {
            location.bearing.toDouble()
        } else {
            // Calculate from previous location
            previousLocation?.let { prev ->
                val distance = calculateDistance(prev.latitude, prev.longitude,
                                                 location.latitude, location.longitude)
                // Only calculate bearing if we've moved at least 5 meters
                if (distance > 5) {
                    calculateBearing(prev.latitude, prev.longitude,
                                     location.latitude, location.longitude)
                } else null
            } ?: 0.0
        }

        // Update previous location for next calculation
        previousLocation = location

        // Build flags object for status indicators
        val flags = JSONObject().apply {
            put("ps", isPowerSaveMode as Boolean)      // Power save mode (system battery saver)
            put("bo", isBatteryOptIgnored as Boolean)  // Battery optimization ignored for this app
        }

        // Get current password and event ID from prefs (allows settings changes to take effect immediately)
        val currentPassword = getCurrentPassword()
        val eventId = getCurrentEventId()

        val packet = JSONObject().apply {
            put("id", sailorId)
            put("eid", eventId)
            put("sq", seq)
            put("ts", System.currentTimeMillis() / 1000)
            put("lat", location.latitude)
            put("lon", location.longitude)
            put("spd", speedMs * 1.94384)  // Convert m/s to knots
            put("hdg", bearing.toInt())
            put("ast", assistRequested.get())
            put("bat", batteryPercent)
            put("chg", isCharging)
            drainRate?.let { put("bdr", String.format("%.1f", it).toDouble()) }
            put("sig", signalLevel)
            put("role", role)
            put("flg", flags)  // Status flags
            put("ver", BuildConfig.VERSION_STRING)
            put("os", "Android ${android.os.Build.VERSION.RELEASE}")
            if (currentPassword.isNotEmpty()) {
                put("pwd", currentPassword)
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

                    packetsSent.incrementAndGet()
                    statusListener?.onPacketSent(seq)

                    Log.d(TAG, "Sent packet seq=$seq attempt=${attempt + 1}")

                    if (attempt < UDP_RETRY_COUNT - 1) {
                        delay(UDP_RETRY_DELAY_MS)
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to send packet", e)
                }
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

        // Get battery level
        val batteryManager = getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val batteryPercent = batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val isCharging = batteryManager.isCharging

        // Calculate battery drain rate (%/hr) - need at least 5 minutes of tracking
        var drainRate: Double? = null
        if (trackingStartTime > 0 && trackingStartBattery >= 0 && batteryPercent >= 0) {
            val elapsedMs = System.currentTimeMillis() - trackingStartTime
            if (elapsedMs >= 5 * 60 * 1000) {  // 5 minutes minimum
                val drainPercent = trackingStartBattery - batteryPercent
                val hoursElapsed = elapsedMs / (1000.0 * 3600.0)
                if (hoursElapsed > 0) {
                    drainRate = drainPercent / hoursElapsed
                }
            }
        }

        // Get power/battery saver status
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        val isPowerSaveMode = powerManager.isPowerSaveMode
        val isBatteryOptIgnored = powerManager.isIgnoringBatteryOptimizations(packageName)

        // Get signal strength
        val signalLevel = try {
            val telephonyManager = getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
            telephonyManager.signalStrength?.level ?: -1
        } catch (e: Exception) {
            -1
        }

        // Get speed from last location
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

        // Build flags object
        val flags = JSONObject().apply {
            put("ps", isPowerSaveMode as Boolean)
            put("bo", isBatteryOptIgnored as Boolean)
        }

        // Build position array: [[ts, lat, lon], ...]
        val posArray = org.json.JSONArray()
        for (pos in positionBuffer) {
            val posEntry = org.json.JSONArray()
            posEntry.put(pos.first)   // ts
            posEntry.put(pos.second)  // lat
            posEntry.put(pos.third)   // lon
            posArray.put(posEntry)
        }

        // Clear buffer after copying
        val numPositions = positionBuffer.size
        positionBuffer.clear()

        // Get current password and event ID from prefs (allows settings changes to take effect immediately)
        val currentPassword = getCurrentPassword()
        val eventId = getCurrentEventId()

        val packet = JSONObject().apply {
            put("id", sailorId)
            put("eid", eventId)
            put("sq", seq)
            put("ts", System.currentTimeMillis() / 1000)
            put("pos", posArray)  // Position array instead of lat/lon
            put("spd", speedMs * 1.94384)  // Convert m/s to knots
            put("hdg", bearing.toInt())
            put("ast", assistRequested.get())
            put("bat", batteryPercent)
            put("chg", isCharging)
            drainRate?.let { put("bdr", String.format("%.1f", it).toDouble()) }
            put("sig", signalLevel)
            put("role", role)
            put("flg", flags)
            put("ver", BuildConfig.VERSION_STRING)
            put("os", "Android ${android.os.Build.VERSION.RELEASE}")
            if (currentPassword.isNotEmpty()) {
                put("pwd", currentPassword)
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

                    packetsSent.incrementAndGet()
                    statusListener?.onPacketSent(seq)

                    Log.d(TAG, "Sent array packet seq=$seq with $numPositions positions, attempt=${attempt + 1}")

                    if (attempt < UDP_RETRY_COUNT - 1) {
                        delay(UDP_RETRY_DELAY_MS)
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to send packet", e)
                }
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
                        // Check for auth error
                        val error = ack.optString("error", "")
                        if (error == "auth") {
                            val msg = ack.optString("msg", "Invalid password")
                            Log.w(TAG, "Auth error received: $msg")
                            statusListener?.onAuthError(msg)
                            // Don't count as successful ACK
                            continue
                        }

                        // Mark this sequence as acknowledged to stop retransmissions
                        acknowledgedSeqs.add(ackSeq)

                        // Clean up old sequence numbers (keep only recent ones)
                        val currentSeq = sequenceNumber.get()
                        acknowledgedSeqs.removeIf { it < currentSeq - 100 }

                        lastAckTime.set(System.currentTimeMillis())
                        packetsAcked.incrementAndGet()

                        val ackRate = packetsAcked.get().toFloat() / maxOf(packetsSent.get(), 1)
                        statusListener?.onAckReceived(ackSeq)
                        statusListener?.onConnectionStatus(ackRate)

                        Log.d(TAG, "Received ACK for seq=$ackSeq")
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
    
    fun requestAssist(enabled: Boolean) {
        assistRequested.set(enabled)
        Log.d(TAG, "Assist ${if (enabled) "ENABLED" else "disabled"}")
        
        // Send immediate position update if requesting assist
        if (enabled) {
            lastLocation?.let { sendPosition(it) }
        }
    }
    
    fun getLastLocation(): Location? = lastLocation
    
    fun getAckRate(): Float {
        val sent = packetsSent.get()
        return if (sent > 0) packetsAcked.get().toFloat() / sent else 0f
    }
    
    fun getLastAckTime(): Long = lastAckTime.get()
    
    fun isAssistActive(): Boolean = assistRequested.get()
    
    fun isTracking(): Boolean = isRunning.get()
}
