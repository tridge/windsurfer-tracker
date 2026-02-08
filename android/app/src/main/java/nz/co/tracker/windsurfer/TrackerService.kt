package nz.co.tracker.windsurfer

import android.app.*
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.BatteryManager
import android.os.Binder
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.telephony.TelephonyManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LifecycleService
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
    
    // Location - using native LocationManager for Direct Boot support
    // (FusedLocationProvider requires Google Play Services which isn't available before unlock)
    private lateinit var locationManager: LocationManager
    private lateinit var locationListener: LocationListener
    private var lastLocation: Location? = null
    private var previousLocation: Location? = null  // For calculating speed/bearing
    private var lastSatelliteCount: Int = 0  // From GnssStatus callback
    private var gnssStatusCallback: android.location.GnssStatus.Callback? = null
    
    // UDP
    private var socket: DatagramSocket? = null
    private var serverHost: String = DEFAULT_SERVER_HOST
    private var serverPort: Int = DEFAULT_SERVER_PORT
    private var sailorId: String = ""
    private var role: String = "sailor"  // sailor, support, spectator
    // Note: password is read from SharedPreferences on each send to pick up changes immediately
    private var highFrequencyMode: Boolean = true  // 1Hz mode - send positions as array

    // 1Hz mode position buffer: [[ts, lat, lon, spd], ...]
    private data class BufferedPosition(val ts: Long, val lat: Double, val lon: Double, val spd: Double)
    private val positionBuffer = mutableListOf<BufferedPosition>()
    private var lastBufferedLocation: Location? = null
    private var firstPacketSent = false  // Track if initial packet sent for quick ACK

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

    // Distance tracking
    private var totalDistance: Float = 0f  // Total distance in meters
    private var distanceStartLocation: Location? = null  // For distance calculation

    // State
    private val isRunning = AtomicBoolean(false)
    private val isIdleMode = AtomicBoolean(false)
    private var idleIntervalMs: Long = 0  // From server ACK "idle" field (0 = disabled)
    private val assistRequested = AtomicBoolean(false)
    private val sequenceNumber = AtomicInteger(0)
    private val lastAckTime = AtomicLong(0)
    private val hasGpsFix = AtomicBoolean(false)
    private val hasFirstAck = AtomicBoolean(false)
    private val hasAuthFailure = AtomicBoolean(false)
    private var currentEventName: String = ""
    // Sliding window for ACK rate calculation (last 20 messages)
    private val ackWindow = java.util.concurrent.ConcurrentLinkedDeque<Boolean>()
    private val ACK_WINDOW_SIZE = 20
    // Track sequences that have been recorded in the window (to avoid double-counting)
    private val recordedSeqs = java.util.concurrent.ConcurrentHashMap.newKeySet<Int>()

    // Track acknowledged sequence numbers to stop retransmissions
    private val acknowledgedSeqs = java.util.concurrent.ConcurrentHashMap.newKeySet<Int>()

    // Tracker beep - plays once per minute to remind user tracker is running
    private var toneGenerator: ToneGenerator? = null
    private val beepHandler = Handler(Looper.getMainLooper())
    private val beepRunnable = object : Runnable {
        override fun run() {
            if (isRunning.get() && isTrackerBeepEnabled()) {
                playTrackerBeep()
            }
            if (isRunning.get()) {
                beepHandler.postDelayed(this, 60000L)  // Every 60 seconds
            }
        }
    }

    // Notification icon update - checks every 5 seconds for ACK status change
    private val notificationHandler = Handler(Looper.getMainLooper())
    private val notificationUpdateRunnable = object : Runnable {
        override fun run() {
            if (isRunning.get()) {
                updateNotificationIconIfNeeded()
                notificationHandler.postDelayed(this, 5000L)  // Every 5 seconds
            }
        }
    }

    // Idle mode wake lock - keeps CPU alive for periodic heartbeats in battery saver
    private var idleWakeLock: PowerManager.WakeLock? = null
    private var idleJob: Job? = null

    // Receiver to restart location updates when user unlocks the device
    // This is needed because Google Play Services (FusedLocationProvider) isn't available during Direct Boot
    private var userUnlockedReceiver: BroadcastReceiver? = null

    // Receiver to send stop packet when device is shutting down
    private var shutdownReceiver: BroadcastReceiver? = null

    /**
     * Update notification icon if ACK status has changed (connected <-> disconnected)
     */
    private fun updateNotificationIconIfNeeded() {
        val lastAck = lastAckTime.get()
        val hasRecentAck = lastAck > 0 && (System.currentTimeMillis() - lastAck) < ACK_TIMEOUT_FOR_ICON_MS

        // Only update if icon state changed
        if (lastNotificationIconOk != hasRecentAck) {
            val text = if (hasRecentAck) "Tracking active" else "Tracking - no connection"
            updateNotification(text)
        }
    }

    // Coroutines
    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    
    // Listener for UI updates
    var statusListener: StatusListener? = null
    
    interface StatusListener {
        fun onLocationUpdate(location: Location, totalDistanceMeters: Float)
        fun onAckReceived(seq: Int)
        fun onPacketSent(seq: Int)
        fun onConnectionStatus(ackRate: Float)
        fun onAuthError(message: String)
        fun onEventName(name: String)
        fun onStatusLine(status: String)  // GPS wait, connecting..., auth failure, or event name
        fun onAssistEnabled(enabled: Boolean)  // Whether assist button should be shown
        fun onRemoteStop()  // Server sent remote stop command
        fun onRemoteCancelAssist()  // Server sent remote cancel assist command
        fun onRemoteStart()  // Server sent start command (from idle mode)
        fun onRemoteShutdown()  // Server sent shutdown command (from idle mode)
    }

    /**
     * Update the status line based on current state.
     * Priority: auth failure > event name > connecting > GPS wait
     */
    private fun updateStatusLine() {
        val status = when {
            hasAuthFailure.get() -> "auth failure"
            hasFirstAck.get() && currentEventName.isNotEmpty() -> currentEventName
            hasGpsFix.get() -> "connecting ..."
            else -> "GPS wait"
        }
        statusListener?.onStatusLine(status)
    }

    /**
     * Get SharedPreferences, using device-protected storage for Direct Boot support.
     */
    private fun getPrefs(): android.content.SharedPreferences {
        // Use device-protected storage so preferences are available during Direct Boot
        val deviceContext = createDeviceProtectedStorageContext()
        return deviceContext.getSharedPreferences("tracker_prefs", Context.MODE_PRIVATE)
    }

    /**
     * Get the current password from SharedPreferences.
     * This is read on each send so settings changes take effect immediately.
     */
    private fun getCurrentPassword(): String {
        return getPrefs().getString("password", "") ?: ""
    }

    /**
     * Get the current event ID from SharedPreferences.
     * This is read on each send so settings changes take effect immediately.
     * Defaults to 1 for backwards compatibility.
     */
    private fun getCurrentEventId(): Int {
        return getPrefs().getInt("event_id", 2)
    }

    /**
     * Check if tracker beep is enabled. Defaults to true.
     */
    private fun isTrackerBeepEnabled(): Boolean {
        return getPrefs().getBoolean("tracker_beep", true)
    }

    /**
     * Save last position to device-protected storage so BootReceiver can send
     * a stop packet on shutdown even if the service is killed first.
     */
    private fun saveLastPosition(location: Location) {
        getPrefs().edit().apply {
            putFloat("last_lat", location.latitude.toFloat())
            putFloat("last_lon", location.longitude.toFloat())
            putLong("last_ts", System.currentTimeMillis())
            apply()
        }
    }

    /**
     * Clear saved last position (called when tracking stops normally).
     */
    private fun clearLastPosition() {
        getPrefs().edit().apply {
            remove("last_lat")
            remove("last_lon")
            remove("last_ts")
            putBoolean("tracking_active", false)
            apply()
        }
    }

    /**
     * Play tracker beep: one buzz if ACK received in last minute, two buzzes if not.
     * Uses vibration since audio may be muted.
     */
    private fun playTrackerBeep() {
        try {
            Log.d(TAG, "Playing tracker beep via vibration...")
            val vibrator = getSystemService(Context.VIBRATOR_SERVICE) as Vibrator

            val lastAck = lastAckTime.get()
            val hasRecentAck = lastAck > 0 && (System.currentTimeMillis() - lastAck) < 60000L
            Log.d(TAG, "hasRecentAck=$hasRecentAck, lastAck=$lastAck")

            if (hasRecentAck) {
                // One buzz - connection OK
                vibrator.vibrate(VibrationEffect.createOneShot(150, VibrationEffect.DEFAULT_AMPLITUDE))
                Log.d(TAG, "Played single buzz (OK)")
            } else {
                // Two buzzes - no connection
                val pattern = longArrayOf(0, 150, 150, 150)  // delay, buzz, pause, buzz
                vibrator.vibrate(VibrationEffect.createWaveform(pattern, -1))
                Log.d(TAG, "Played double buzz (no connection)")
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to play tracker beep: ${e.message}")
        }
    }

    override fun onCreate() {
        super.onCreate()
        Log.d(TAG, "Service created")

        // Use native LocationManager instead of FusedLocationProvider for Direct Boot support
        locationManager = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        createNotificationChannel()
        setupLocationListener()
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
            highFrequencyMode = it.getBooleanExtra("high_frequency_mode", true)
            // Clear position buffer when mode changes
            positionBuffer.clear()
            firstPacketSent = false  // Reset when buffer cleared
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
        Log.i(TAG, "onDestroy called, isRunning=${isRunning.get()}, isIdle=${isIdleMode.get()}")
        super.onDestroy()
        // Send stop packet before cleaning up (catches shutdown/reboot)
        if (isRunning.get()) {
            Log.i(TAG, "Service being destroyed while tracking, sending stop packet")
            sendStopPacketSync()
        }
        // Clean up idle mode if active
        if (isIdleMode.get()) {
            idleJob?.cancel()
            idleJob = null
            idleWakeLock?.let { if (it.isHeld) it.release() }
            idleWakeLock = null
            isIdleMode.set(false)
        }
        stopTracking()
        serviceScope.cancel()
        Log.d(TAG, "Service destroyed")
    }
    
    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Tracker Service",
            NotificationManager.IMPORTANCE_DEFAULT
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
    
    // Timeout for considering connection "ok" (30 seconds)
    private val ACK_TIMEOUT_FOR_ICON_MS = 30000L
    // Track last icon state to avoid unnecessary notification updates
    private var lastNotificationIconOk: Boolean? = null

    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        // Choose icon based on ACK status
        val lastAck = lastAckTime.get()
        val hasRecentAck = lastAck > 0 && (System.currentTimeMillis() - lastAck) < ACK_TIMEOUT_FOR_ICON_MS
        val iconRes = if (hasRecentAck) R.drawable.ic_windsurfer_ok else R.drawable.ic_windsurfer_error
        lastNotificationIconOk = hasRecentAck

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Windsurfer Tracker")
            .setContentText(text)
            .setSmallIcon(iconRes)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)  // Show on lock screen
            .build()
    }
    
    private fun updateNotification(text: String) {
        val notification = buildNotification(text)
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, notification)
    }
    
    private fun setupLocationListener() {
        locationListener = object : LocationListener {
            override fun onLocationChanged(location: Location) {
                handleLocationUpdate(location)
            }

            override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {
                Log.d(TAG, "Location provider $provider status changed: $status")
            }

            override fun onProviderEnabled(provider: String) {
                Log.d(TAG, "Location provider enabled: $provider")
            }

            override fun onProviderDisabled(provider: String) {
                Log.w(TAG, "Location provider disabled: $provider")
            }
        }
    }

    private fun handleLocationUpdate(location: Location) {
        // Filter out invalid 0,0 locations (can happen before GPS is ready)
        if (location.latitude == 0.0 && location.longitude == 0.0) {
            Log.d(TAG, "Skipping invalid 0,0 location - GPS not ready")
            return
        }

        // Filter out locations without accuracy (likely not a real GPS fix)
        if (!location.hasAccuracy()) {
            Log.d(TAG, "Skipping location without accuracy data")
            return
        }

        // Filter out inaccurate locations (technique from OwnTracks)
        if (MAX_ACCURACY_METERS > 0 && location.accuracy > MAX_ACCURACY_METERS) {
            Log.d(TAG, "Skipping inaccurate location: accuracy=${location.accuracy}m > ${MAX_ACCURACY_METERS}m")
            return
        }

        lastLocation = location

        // Save last position to device-protected storage for shutdown stop packet
        saveLastPosition(location)

        // Calculate distance traveled
        distanceStartLocation?.let { prevLoc ->
            val distanceResult = FloatArray(1)
            Location.distanceBetween(
                prevLoc.latitude, prevLoc.longitude,
                location.latitude, location.longitude,
                distanceResult
            )
            val distance = distanceResult[0]
            // Filter out GPS noise (too small) and jumps (too large)
            if (distance > 0.1f && distance < 500f) {
                totalDistance += distance
            }
        }
        distanceStartLocation = location

        statusListener?.onLocationUpdate(location, totalDistance)

        // Mark GPS as ready and update status line
        if (!hasGpsFix.getAndSet(true)) {
            updateStatusLine()  // Show "connecting ..."
        }

        if (highFrequencyMode) {
            // Buffer position for batched sending
            val ts = System.currentTimeMillis() / 1000
            val speedKnots = if (location.hasSpeed() && location.speed > 0) {
                (location.speed * 1.94384 * 10).toInt() / 10.0  // Round to 1 decimal
            } else 0.0
            positionBuffer.add(BufferedPosition(ts, location.latitude, location.longitude, speedKnots))
            lastBufferedLocation = location

            // Send first packet immediately to get quick ACK, then batch every 10 positions
            if (!firstPacketSent && positionBuffer.size >= 1) {
                // First GPS lock - send immediately (even if only 1 position)
                sendPositionArray()
                firstPacketSent = true
            } else if (positionBuffer.size >= 10) {
                // Subsequent packets - send every 10 positions (10 seconds at 1Hz)
                sendPositionArray()
            }
        } else {
            sendPosition(location)
        }
    }
    
    @Suppress("MissingPermission")  // Permission checked in MainActivity
    private fun startTracking() {
        // Exit idle mode if currently idle
        if (isIdleMode.get()) {
            Log.i(TAG, "Exiting idle mode to start tracking")
            idleJob?.cancel()
            idleJob = null
            idleWakeLock?.let { if (it.isHeld) it.release() }
            idleWakeLock = null
            isIdleMode.set(false)
            // Don't close socket - we'll reuse it
        }

        if (isRunning.getAndSet(true)) {
            Log.d(TAG, "Already tracking")
            return
        }

        // Clear acknowledged sequences from previous session
        acknowledgedSeqs.clear()

        // Reset status tracking for new session
        hasGpsFix.set(false)
        hasFirstAck.set(false)
        hasAuthFailure.set(false)
        currentEventName = ""
        totalDistance = 0f
        distanceStartLocation = null
        firstPacketSent = false  // Reset for new session to send first packet immediately
        updateStatusLine()  // Show "GPS wait"

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

        // Initialize socket (skip if already open from idle mode transition)
        if (socket == null || socket?.isClosed == true) {
            serviceScope.launch {
                try {
                    socket = DatagramSocket()
                    socket?.soTimeout = ACK_TIMEOUT_MS.toInt()
                    startAckListener()
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to create socket", e)
                }
            }
        } else {
            Log.d(TAG, "Reusing existing socket from idle mode")
        }

        // Start location updates using native LocationManager (works during Direct Boot)
        val intervalMs = if (highFrequencyMode) 1000L else LOCATION_INTERVAL_MS

        try {
            // Use GPS_PROVIDER directly - doesn't require Google Play Services
            locationManager.requestLocationUpdates(
                LocationManager.GPS_PROVIDER,
                intervalMs,
                0f,  // minDistance - we want time-based updates
                locationListener,
                Looper.getMainLooper()
            )
            Log.i(TAG, "Started GPS location updates with interval ${intervalMs}ms")

            // Register GNSS status callback to track satellite count
            gnssStatusCallback = object : android.location.GnssStatus.Callback() {
                override fun onSatelliteStatusChanged(status: android.location.GnssStatus) {
                    var usedInFix = 0
                    for (i in 0 until status.satelliteCount) {
                        if (status.usedInFix(i)) usedInFix++
                    }
                    lastSatelliteCount = usedInFix
                    Log.d(TAG, "GNSS status: ${status.satelliteCount} visible, $usedInFix used in fix")
                }
            }
            try {
                locationManager.registerGnssStatusCallback(mainExecutor, gnssStatusCallback!!)
                Log.i(TAG, "Registered GNSS status callback for satellite count")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to register GNSS status callback", e)
            }

            updateNotification("Tracking active")

            // Start tracker beep timer (first beep after 60 seconds)
            beepHandler.postDelayed(beepRunnable, 60000L)

            // Start notification icon update timer (first check after 5 seconds)
            notificationHandler.postDelayed(notificationUpdateRunnable, 5000L)

            // Register for shutdown to send stop packet before power off
            registerShutdownReceiver()
        } catch (e: SecurityException) {
            Log.e(TAG, "Location permission denied", e)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start location updates", e)
        }
    }

    /**
     * Register a receiver for ACTION_SHUTDOWN to send a stop packet
     * when the device is being powered off deliberately.
     */
    private fun registerShutdownReceiver() {
        if (shutdownReceiver != null) return  // Already registered

        shutdownReceiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                Log.i(TAG, "Received ${intent.action}, sending stop packet")
                sendStopPacketSync()
            }
        }

        val filter = IntentFilter().apply {
            addAction(Intent.ACTION_SHUTDOWN)
            addAction("android.intent.action.QUICKBOOT_POWEROFF")  // Some devices use this
            addAction("com.htc.intent.action.QUICKBOOT_POWEROFF")  // HTC
        }
        registerReceiver(shutdownReceiver, filter, Context.RECEIVER_EXPORTED)
        Log.d(TAG, "Registered SHUTDOWN receiver")
    }

    /**
     * Send stop packet synchronously (blocking). Used during shutdown
     * when we can't use coroutines and need to send immediately.
     * Sends minimal packet without position (server accepts this for stop).
     */
    private fun sendStopPacketSync() {
        if (sailorId.isEmpty()) {
            Log.w(TAG, "No sailor ID, cannot send stop packet")
            return
        }

        val seq = sequenceNumber.incrementAndGet()
        val currentPassword = getCurrentPassword()
        val eventId = getCurrentEventId()

        val packet = JSONObject().apply {
            put("id", sailorId)
            put("eid", eventId)
            put("sq", seq)
            put("ts", System.currentTimeMillis() / 1000)
            put("stopped", true)  // Deliberate stop
            put("ver", BuildConfig.VERSION_STRING)
            if (currentPassword.isNotEmpty()) {
                put("pwd", currentPassword)
            }
        }

        val data = packet.toString().toByteArray(Charsets.UTF_8)

        try {
            val address = cachedServerAddress
            if (address == null) {
                Log.w(TAG, "No cached server address for stop packet")
                return
            }
            val dgram = DatagramPacket(data, data.size, address, serverPort)
            socket?.send(dgram)
            Log.i(TAG, "Sent shutdown stop packet to $serverHost:$serverPort")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to send shutdown stop packet", e)
        }
    }

    private fun stopTracking() {
        if (!isRunning.getAndSet(false)) return

        Log.d(TAG, "Stopping tracking")

        // Stop tracker beep timer
        beepHandler.removeCallbacks(beepRunnable)
        toneGenerator?.release()
        toneGenerator = null

        // Stop notification update timer
        notificationHandler.removeCallbacks(notificationUpdateRunnable)

        // Unregister shutdown receiver
        shutdownReceiver?.let {
            try {
                unregisterReceiver(it)
            } catch (e: Exception) {
                Log.w(TAG, "Error unregistering shutdown receiver: ${e.message}")
            }
            shutdownReceiver = null
        }

        // Stop location updates
        try {
            locationManager.removeUpdates(locationListener)
        } catch (e: Exception) {
            Log.w(TAG, "Error removing location updates: ${e.message}")
        }
        gnssStatusCallback?.let {
            locationManager.unregisterGnssStatusCallback(it)
            gnssStatusCallback = null
        }
        lastSatelliteCount = 0

        socket?.close()
        socket = null

        // Remove the foreground notification and stop the service
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    /**
     * Send a stop notification packet to the server.
     * This tells the server the user deliberately stopped tracking (vs losing signal).
     * Retries until ACK received or max attempts reached.
     */
    suspend fun sendStopPacket(): Boolean {
        val location = lastLocation ?: return false
        val seq = sequenceNumber.incrementAndGet()

        // Get current password and event ID from prefs
        val currentPassword = getCurrentPassword()
        val eventId = getCurrentEventId()

        val packet = JSONObject().apply {
            put("id", sailorId)
            put("eid", eventId)
            put("sq", seq)
            put("ts", System.currentTimeMillis() / 1000)
            put("lat", location.latitude)
            put("lon", location.longitude)
            put("spd", 0.0)
            put("hdg", 0)
            put("ast", false)  // Clear assist on stop
            put("stopped", true)  // This is a deliberate stop
            put("role", role)
            put("ver", BuildConfig.VERSION_STRING)
            put("os", "Android ${android.os.Build.VERSION.RELEASE}")
            if (currentPassword.isNotEmpty()) {
                put("pwd", currentPassword)
            }
        }

        val data = packet.toString().toByteArray(Charsets.UTF_8)
        val address = getServerAddress() ?: return false

        Log.d(TAG, "Sending stop packet seq=$seq")

        // Try up to 5 times with shorter timeout for stop packet
        repeat(5) { attempt ->
            if (acknowledgedSeqs.contains(seq)) {
                Log.d(TAG, "Stop packet acknowledged")
                return true
            }

            try {
                val dgram = DatagramPacket(data, data.size, address, serverPort)
                socket?.send(dgram)
                Log.d(TAG, "Sent stop packet attempt ${attempt + 1}")

                // Wait for ACK with short timeout
                delay(500)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to send stop packet", e)
            }
        }

        // Check one more time if we got an ACK
        return acknowledgedSeqs.contains(seq).also {
            if (it) Log.d(TAG, "Stop packet acknowledged (delayed)"
            ) else Log.w(TAG, "Stop packet not acknowledged after all attempts")
        }
    }

    /**
     * Request a graceful stop - sends stop notification to server before stopping.
     * This should be called when user deliberately stops tracking.
     */
    fun requestGracefulStop(callback: (() -> Unit)? = null) {
        if (!isRunning.get()) {
            callback?.invoke()
            return
        }

        serviceScope.launch {
            sendStopPacket()
            // Clear saved position since we sent the stop packet successfully
            clearLastPosition()
            withContext(Dispatchers.Main) {
                // If idle mode is supported, enter idle instead of full stop
                if (idleIntervalMs > 0) {
                    enterIdleMode()
                    // Don't call callback - service stays running in idle mode
                } else {
                    stopTracking()
                    callback?.invoke()
                }
            }
        }
    }
    
    /**
     * Enter idle mode: stop GPS, keep socket, send heartbeats at server-configured interval.
     * Called when user stops tracking and server has idle support enabled.
     */
    fun enterIdleMode() {
        if (isIdleMode.get()) return

        Log.i(TAG, "Entering idle mode (interval=${idleIntervalMs}ms)")
        isIdleMode.set(true)
        isRunning.set(false)  // Allow startTracking() to work when admin sends start command

        // Stop GPS updates
        try {
            locationManager.removeUpdates(locationListener)
        } catch (e: Exception) {
            Log.w(TAG, "Error removing location updates for idle mode: ${e.message}")
        }
        gnssStatusCallback?.let {
            locationManager.unregisterGnssStatusCallback(it)
            gnssStatusCallback = null
        }
        lastSatelliteCount = 0

        // Stop beep timer and notification timer
        beepHandler.removeCallbacks(beepRunnable)
        notificationHandler.removeCallbacks(notificationUpdateRunnable)

        // Update notification
        updateNotification("Idle - waiting for admin start")

        // Acquire partial wake lock so CPU wakes up for heartbeats in battery saver
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        idleWakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "WindsurferTracker::IdleHeartbeat").apply {
            acquire()
        }

        // Start idle heartbeat loop in a coroutine (Handler.postDelayed gets throttled by battery saver)
        idleJob = serviceScope.launch {
            while (isIdleMode.get() && idleIntervalMs > 0) {
                sendIdlePacket()
                delay(idleIntervalMs)
            }
        }
    }

    /**
     * Exit idle mode: stop heartbeats, close socket, stop service.
     */
    fun exitIdleMode() {
        if (!isIdleMode.getAndSet(false)) return

        Log.i(TAG, "Exiting idle mode")

        // Cancel idle heartbeat coroutine and release wake lock
        idleJob?.cancel()
        idleJob = null
        idleWakeLock?.let {
            if (it.isHeld) it.release()
        }
        idleWakeLock = null

        // Close socket and stop service
        isRunning.set(false)
        socket?.close()
        socket = null

        // Unregister shutdown receiver
        shutdownReceiver?.let {
            try {
                unregisterReceiver(it)
            } catch (e: Exception) {
                Log.w(TAG, "Error unregistering shutdown receiver: ${e.message}")
            }
            shutdownReceiver = null
        }

        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    /**
     * Send an idle heartbeat packet (no GPS data, no retries).
     */
    private suspend fun sendIdlePacket() {
        val seq = sequenceNumber.incrementAndGet()
        val currentPassword = getCurrentPassword()
        val eventId = getCurrentEventId()

        // Get battery and signal
        val batteryManager = getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val batteryPercent = batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val signalLevel = try {
            val telephonyManager = getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
            telephonyManager.signalStrength?.level ?: -1
        } catch (e: Exception) { -1 }

        val packet = JSONObject().apply {
            put("id", sailorId)
            put("eid", eventId)
            put("sq", seq)
            put("ts", System.currentTimeMillis() / 1000)
            put("idle", true)
            put("bat", batteryPercent)
            put("sig", signalLevel)
            put("role", role)
            put("ver", BuildConfig.VERSION_STRING)
            put("os", "Android ${android.os.Build.VERSION.RELEASE}")
            if (currentPassword.isNotEmpty()) {
                put("pwd", currentPassword)
            }
        }

        val data = packet.toString().toByteArray(Charsets.UTF_8)
        val address = getServerAddress() ?: return

        try {
            val dgram = DatagramPacket(data, data.size, address, serverPort)
            socket?.send(dgram)
            Log.d(TAG, "Sent idle heartbeat seq=$seq bat=$batteryPercent%")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to send idle heartbeat", e)
        }
    }

    fun isIdle(): Boolean = isIdleMode.get()

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
            if (location.hasAccuracy()) {
                put("hac", String.format("%.2f", location.accuracy).toDouble())  // Horizontal accuracy in meters
            }
            if (lastSatelliteCount > 0) put("nsats", lastSatelliteCount)
            put("spd", String.format("%.2f", speedMs * 1.94384).toDouble())  // Convert m/s to knots
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
            if (location.hasAccuracy()) {
                put("hac", String.format("%.2f", location.accuracy).toDouble())  // Horizontal accuracy in meters
            }
            if (lastSatelliteCount > 0) put("nsats", lastSatelliteCount)
            put("spd", String.format("%.2f", speedMs * 1.94384).toDouble())  // Convert m/s to knots
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

            while (isRunning.get() || isIdleMode.get()) {
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
                            hasAuthFailure.set(true)
                            updateStatusLine()  // Show "auth failure"
                            statusListener?.onAuthError(msg)
                            // Don't count as successful ACK
                            continue
                        }

                        // Clear auth failure on successful ACK
                        hasAuthFailure.set(false)

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

                        // Check for event name in ACK and update status
                        val eventName = ack.optString("event", "")
                        if (eventName.isNotEmpty()) {
                            currentEventName = eventName
                            hasFirstAck.set(true)
                            updateStatusLine()  // Show event name
                            statusListener?.onEventName(eventName)
                        } else if (!hasFirstAck.get()) {
                            // First ACK but no event name yet
                            hasFirstAck.set(true)
                            updateStatusLine()
                        }

                        // Check for assist enabled status (missing = true, explicit false = disabled)
                        if (ack.has("assist")) {
                            val assistEnabled = ack.optBoolean("assist", true)
                            statusListener?.onAssistEnabled(assistEnabled)
                            // Clear local assist flag if server says assist is disabled
                            if (!assistEnabled && assistRequested.getAndSet(false)) {
                                Log.d(TAG, "Assist cleared by server (assist disabled for event)")
                            }
                        } else {
                            // Default to enabled if not specified
                            statusListener?.onAssistEnabled(true)
                        }

                        // Cache idle interval from server ACK
                        if (ack.has("idle")) {
                            val interval = ack.optInt("idle", 0)
                            idleIntervalMs = interval * 1000L
                        }

                        // Check for remote commands
                        val cmd = ack.optString("cmd", "")
                        if (cmd == "stop") {
                            Log.w(TAG, "Received remote STOP command from server")
                            // Send stop packet to server, then notify UI and stop
                            requestGracefulStop {
                                Handler(Looper.getMainLooper()).post {
                                    statusListener?.onRemoteStop()
                                }
                            }
                        } else if (cmd == "cancel_assist") {
                            Log.w(TAG, "Received remote CANCEL ASSIST command from server")
                            // Cancel assist as if user cancelled it
                            assistRequested.set(false)
                            Handler(Looper.getMainLooper()).post {
                                statusListener?.onRemoteCancelAssist()
                            }
                        } else if (cmd == "start") {
                            if (isIdleMode.get()) {
                                Log.w(TAG, "Received remote START command from server")
                                // Start tracking directly from the service (don't go through Activity intent flow)
                                Handler(Looper.getMainLooper()).post {
                                    startTracking()
                                    statusListener?.onRemoteStart()
                                }
                            } else {
                                Log.d(TAG, "Ignoring start command - already actively tracking")
                            }
                        } else if (cmd == "shutdown") {
                            if (isIdleMode.get()) {
                                Log.w(TAG, "Received remote SHUTDOWN command from server")
                                Handler(Looper.getMainLooper()).post {
                                    exitIdleMode()
                                    statusListener?.onRemoteShutdown()
                                }
                            } else {
                                Log.d(TAG, "Ignoring shutdown command - actively tracking")
                            }
                        }

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
    
    fun isAssistActive(): Boolean = assistRequested.get()
    
    fun isTracking(): Boolean = isRunning.get()
}
