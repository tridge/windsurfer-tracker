package nz.co.tracker.windsurfer

import android.app.*
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.Network
import android.content.pm.ServiceInfo
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
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
import kotlinx.coroutines.sync.withLock
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
        const val WSTRACKER_ORG_IPV4 = "103.230.158.49"
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

    // 1Hz position buffer: [[ts, lat, lon, spd], ...]
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
                } else if (serverHost == "wstracker.org") {
                    // Hardcoded fallback for wstracker.org when DNS is unavailable
                    val fallback = InetAddress.getByName(WSTRACKER_ORG_IPV4)
                    cachedServerAddress = fallback
                    lastDnsLookupTime = 0  // Retry DNS on next call
                    Log.w(TAG, "DNS failed, using hardcoded fallback $WSTRACKER_ORG_IPV4 for wstracker.org")
                    return fallback
                } else {
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
    private var keepaliveJob: Job? = null
    private val lastPositionSendTime = AtomicLong(0)  // Track last real position send

    // ACK listener job - tracked to prevent duplicate listeners
    private var ackListenerJob: Job? = null

    // Network change callback - detects WiFi/cellular transitions to invalidate stale sockets
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    // Mutex for socket creation to prevent races
    private val socketMutex = kotlinx.coroutines.sync.Mutex()

    // Volume button assist: AccessibilityService (VolumeKeyService) detects combo when
    // screen is on; MediaSession VolumeProvider detects it when screen is off (since
    // PhoneWindowManager routes locked-screen volume keys to MediaSessionService, bypassing
    // the accessibility input pipeline entirely).
    private var lastComboTriggerTime = 0L
    private var lastVolumeUpChangeTime = 0L
    private var lastVolumeDownChangeTime = 0L
    private var mediaSession: android.media.session.MediaSession? = null
    private var volumeChangeReceiver: BroadcastReceiver? = null
    private var silentAudioTrack: AudioTrack? = null

    // Assist active alarm: plays triple ascending tones every 5 seconds while assist is active
    private val assistAlarmHandler = Handler(Looper.getMainLooper())
    private val assistAlarmRunnable = object : Runnable {
        override fun run() {
            if (assistRequested.get() && isRunning.get()) {
                playAssistTones(ascending = true)
                assistAlarmHandler.postDelayed(this, 5000L)
            }
        }
    }

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
        fun onIdleEntered()  // Entered idle mode after user stop (UI should show config screen)
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

    /**
     * Play triple ascending or descending tones at max volume for assist feedback.
     * Ascending (low→mid→high) = assist activated, descending = deactivated.
     */
    private fun playAssistTones(ascending: Boolean) {
        // Generate all three tones as a single PCM buffer played through one AudioTrack.
        // This eliminates gaps caused by ToneGenerator.startTone() latency.
        Thread {
            try {
                val sampleRate = 44100
                val toneSamples = sampleRate * 150 / 1000   // 150ms per tone
                val gapSamples = sampleRate * 50 / 1000     // 50ms gap
                val totalSamples = toneSamples * 3 + gapSamples * 2

                // DTMF dual-tone frequencies: 1=(697+1209), 5=(770+1336), 9=(852+1477)
                val freqs = if (ascending) {
                    arrayOf(697.0 to 1209.0, 770.0 to 1336.0, 852.0 to 1477.0)
                } else {
                    arrayOf(852.0 to 1477.0, 770.0 to 1336.0, 697.0 to 1209.0)
                }

                val buffer = ShortArray(totalSamples)
                var offset = 0
                for ((i, freq) in freqs.withIndex()) {
                    for (s in 0 until toneSamples) {
                        val t = s.toDouble() / sampleRate
                        val sample = (Math.sin(2 * Math.PI * freq.first * t) +
                                Math.sin(2 * Math.PI * freq.second * t)) * 0.4 * Short.MAX_VALUE
                        buffer[offset + s] = sample.toInt().toShort()
                    }
                    offset += toneSamples
                    if (i < 2) offset += gapSamples  // gap is zeros = silence
                }

                val track = AudioTrack.Builder()
                    .setAudioAttributes(AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build())
                    .setAudioFormat(AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(sampleRate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build())
                    .setBufferSizeInBytes(totalSamples * 2)
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .build()

                track.write(buffer, 0, buffer.size)
                track.play()
                Thread.sleep((150 * 3 + 50 * 2 + 50).toLong())
                track.stop()
                track.release()
            } catch (e: Exception) {
                Log.w(TAG, "Failed to play assist tones: ${e.message}")
            }
        }.start()
    }

    /**
     * Play rising (start) or falling (stop) dual tone for tracking state changes.
     * Uses the same PCM AudioTrack approach as assist tones for reliable playback.
     */
    private fun playTrackingTones(ascending: Boolean) {
        Thread {
            try {
                val sampleRate = 44100
                val toneSamples = sampleRate * 150 / 1000   // 150ms per tone
                val gapSamples = sampleRate * 50 / 1000     // 50ms gap
                val totalSamples = toneSamples * 2 + gapSamples

                // DTMF dual-tone frequencies: 1=(697+1209), 9=(852+1477)
                val freqs = if (ascending) {
                    arrayOf(697.0 to 1209.0, 852.0 to 1477.0)
                } else {
                    arrayOf(852.0 to 1477.0, 697.0 to 1209.0)
                }

                val buffer = ShortArray(totalSamples)
                var offset = 0
                for ((i, freq) in freqs.withIndex()) {
                    for (s in 0 until toneSamples) {
                        val t = s.toDouble() / sampleRate
                        val sample = (Math.sin(2 * Math.PI * freq.first * t) +
                                Math.sin(2 * Math.PI * freq.second * t)) * 0.4 * Short.MAX_VALUE
                        buffer[offset + s] = sample.toInt().toShort()
                    }
                    offset += toneSamples
                    if (i < 1) offset += gapSamples
                }

                val track = AudioTrack.Builder()
                    .setAudioAttributes(AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build())
                    .setAudioFormat(AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(sampleRate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build())
                    .setBufferSizeInBytes(totalSamples * 2)
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .build()

                track.write(buffer, 0, buffer.size)
                track.play()
                Thread.sleep((150 * 2 + 50 + 50).toLong())
                track.stop()
                track.release()
            } catch (e: Exception) {
                Log.w(TAG, "Failed to play tracking tones: ${e.message}")
            }
        }.start()
    }

    /**
     * Handle volume combo detected by VolumeKeyService or volume observer.
     * Toggles assist state, vibrates, and plays tones.
     */
    private fun handleVolumeCombo() {
        // Debounce: ignore if triggered recently (prevents double-trigger from
        // AccessibilityService + ContentObserver both detecting the same combo)
        val now = android.os.SystemClock.uptimeMillis()
        if (now - lastComboTriggerTime < 1000L) return
        lastComboTriggerTime = now

        if (!isRunning.get() && !isIdleMode.get()) return

        val newAssist = !assistRequested.get()

        if (newAssist && isIdleMode.get()) {
            startTrackingFromIdle()
            statusListener?.onRemoteStart()
        }

        requestAssist(newAssist)

        try {
            val vibrator = getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
            val duration = if (newAssist) 500L else 150L
            vibrator.vibrate(
                VibrationEffect.createOneShot(duration, VibrationEffect.DEFAULT_AMPLITUDE)
            )
        } catch (e: Exception) {
            Log.w(TAG, "Failed to vibrate for assist toggle: ${e.message}")
        }

        Log.i(TAG, "Volume button assist toggled: assist=$newAssist")
    }

    /**
     * Register volume combo detection via three complementary mechanisms:
     * 1. AccessibilityService onKeyEvent() — works when screen is on (all devices)
     * 2. MediaSession VolumeProvider — works on Android 10 where MediaSessionService
     *    routes volume keys to our session when screen is off
     * 3. VOLUME_CHANGED_ACTION broadcast — works on Android 12+ where the MediaSession
     *    is skipped (requires MediaRouter2 routing session) and volume keys fall through
     *    to AudioService which sends the broadcast
     *
     * The 1-second debounce in handleVolumeCombo() prevents double-triggers when
     * multiple mechanisms fire for the same combo.
     */
    private fun setupVolumeAssist() {
        // 1. AccessibilityService callback
        VolumeKeyService.onVolumeComboDetected = { handleVolumeCombo() }

        // 2. MediaSession with VolumeProvider (works on Android 10, skipped on 12+)
        val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val session = android.media.session.MediaSession(this, "WindsurferAssist")
        session.setPlaybackToRemote(object : android.media.VolumeProvider(
            VOLUME_CONTROL_RELATIVE,
            am.getStreamMaxVolume(AudioManager.STREAM_MUSIC),
            am.getStreamVolume(AudioManager.STREAM_MUSIC)
        ) {
            override fun onAdjustVolume(direction: Int) {
                // Forward volume change so keys still work normally
                if (direction != 0) {
                    am.adjustStreamVolume(
                        AudioManager.STREAM_MUSIC,
                        if (direction > 0) AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER,
                        0
                    )
                }
                // Combo detection
                val now = android.os.SystemClock.uptimeMillis()
                if (direction > 0) {
                    lastVolumeUpChangeTime = now
                    if (lastVolumeDownChangeTime > 0 && (now - lastVolumeDownChangeTime) <= 800) {
                        Log.i(TAG, "Volume combo detected via MediaSession (up after down)")
                        handleVolumeCombo()
                    }
                } else if (direction < 0) {
                    lastVolumeDownChangeTime = now
                    if (lastVolumeUpChangeTime > 0 && (now - lastVolumeUpChangeTime) <= 800) {
                        Log.i(TAG, "Volume combo detected via MediaSession (down after up)")
                        handleVolumeCombo()
                    }
                }
            }
        })
        // A Callback is required for the internal CallbackMessageHandler to exist —
        // without it, dispatchAdjustVolume() silently drops events.
        session.setCallback(object : android.media.session.MediaSession.Callback() {},
            Handler(Looper.getMainLooper()))
        // A PlaybackState is required for MediaSessionService to consider this session
        // when routing volume key events (sessions without state are skipped).
        session.setPlaybackState(
            android.media.session.PlaybackState.Builder()
                .setState(android.media.session.PlaybackState.STATE_PLAYING, 0, 0f)
                .build()
        )
        session.isActive = true
        mediaSession = session

        // 3a. Silent AudioTrack on STREAM_MUSIC — makes system see "music is playing"
        // so screen-off volume events (musicOnly=true) aren't skipped.
        // Without this, Android 12+ says "Nothing is playing on the music stream"
        // and drops volume key events entirely when screen is blanked.
        try {
            val sampleRate = 8000
            val bufSize = AudioTrack.getMinBufferSize(
                sampleRate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val track = AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build())
                .setAudioFormat(AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(sampleRate)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build())
                .setBufferSizeInBytes(bufSize)
                .setTransferMode(AudioTrack.MODE_STATIC)
                .build()
            val silence = ShortArray(bufSize / 2)  // All zeros = silence
            track.write(silence, 0, silence.size)
            track.setLoopPoints(0, silence.size, -1)  // Loop forever
            track.play()
            silentAudioTrack = track
            Log.i(TAG, "Started silent AudioTrack for volume key detection")
        } catch (e: Exception) {
            Log.w(TAG, "Failed to start silent AudioTrack: ${e.message}")
        }

        // 3b. VOLUME_CHANGED_ACTION broadcast (works on Android 12+ where MediaSession
        // is skipped and volume keys fall through to AudioService)
        volumeChangeReceiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                val newVol = intent.getIntExtra("android.media.EXTRA_VOLUME_STREAM_VALUE", -1)
                val prevVol = intent.getIntExtra("android.media.EXTRA_PREV_VOLUME_STREAM_VALUE", -1)
                if (newVol < 0 || prevVol < 0 || newVol == prevVol) return
                val now = android.os.SystemClock.uptimeMillis()
                if (newVol > prevVol) {
                    lastVolumeUpChangeTime = now
                    if (lastVolumeDownChangeTime > 0 && (now - lastVolumeDownChangeTime) <= 800) {
                        Log.i(TAG, "Volume combo detected via broadcast (up after down)")
                        handleVolumeCombo()
                    }
                } else {
                    lastVolumeDownChangeTime = now
                    if (lastVolumeUpChangeTime > 0 && (now - lastVolumeUpChangeTime) <= 800) {
                        Log.i(TAG, "Volume combo detected via broadcast (down after up)")
                        handleVolumeCombo()
                    }
                }
            }
        }
        registerReceiver(volumeChangeReceiver, IntentFilter("android.media.VOLUME_CHANGED_ACTION"))

        Log.i(TAG, "Registered volume combo: AccessibilityService + MediaSession + broadcast")
    }

    private fun teardownVolumeAssist() {
        VolumeKeyService.onVolumeComboDetected = null
        mediaSession?.isActive = false
        mediaSession?.release()
        mediaSession = null
        silentAudioTrack?.let {
            try { it.stop(); it.release() } catch (_: Exception) {}
        }
        silentAudioTrack = null
        volumeChangeReceiver?.let {
            try { unregisterReceiver(it) } catch (_: Exception) {}
        }
        volumeChangeReceiver = null
    }

    override fun onCreate() {
        super.onCreate()
        Log.d(TAG, "Service created")

        // Use native LocationManager instead of FusedLocationProvider for Direct Boot support
        locationManager = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        createNotificationChannel()
        setupLocationListener()
        setupVolumeAssist()
    }
    
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        
        // Extract configuration from intent, or fall back to SharedPreferences
        // when START_STICKY restarts the service with a null intent
        if (intent != null) {
            serverHost = intent.getStringExtra("server_host") ?: DEFAULT_SERVER_HOST
            serverPort = intent.getIntExtra("server_port", DEFAULT_SERVER_PORT)
            sailorId = intent.getStringExtra("sailor_id") ?: ""
            role = intent.getStringExtra("role") ?: "sailor"
            // Password is read from SharedPreferences on each send (not cached)
            positionBuffer.clear()
            firstPacketSent = false  // Reset when buffer cleared
        } else {
            // Service restarted by system (START_STICKY) - recover config from prefs
            val prefs = getPrefs()
            serverHost = prefs.getString("server_host", DEFAULT_SERVER_HOST) ?: DEFAULT_SERVER_HOST
            serverPort = prefs.getInt("server_port", DEFAULT_SERVER_PORT)
            sailorId = prefs.getString("sailor_id", "") ?: ""
            role = prefs.getString("role", "sailor") ?: "sailor"
            Log.w(TAG, "Service restarted with null intent, recovered config from prefs: id=$sailorId")
        }
        
        startForegroundService()

        if (intent?.getBooleanExtra("start_in_idle", false) == true) {
            startInIdleMode()
        } else {
            startTracking()
        }

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
        // Clean up volume assist
        teardownVolumeAssist()
        // Clean up network callback and ACK listener
        unregisterNetworkCallback()
        ackListenerJob?.cancel()
        ackListenerJob = null
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

        // Buffer position for batched sending (1Hz mode)
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
    }
    
    /**
     * Ensure a healthy UDP socket exists. Creates a new one if null or closed,
     * and starts the ACK listener. Thread-safe via coroutine Mutex.
     */
    private suspend fun ensureSocket(): DatagramSocket? {
        val s = socket
        if (s != null && !s.isClosed) return s
        return socketMutex.withLock {
            // Re-check inside lock
            val s2 = socket
            if (s2 != null && !s2.isClosed) return@withLock s2
            try {
                val newSocket = DatagramSocket()
                newSocket.soTimeout = ACK_TIMEOUT_MS.toInt()
                socket = newSocket
                startAckListener()
                Log.i(TAG, "Created new UDP socket")
                newSocket
            } catch (e: Exception) {
                Log.e(TAG, "Failed to create socket", e)
                null
            }
        }
    }

    /**
     * Register a ConnectivityManager callback to detect network changes.
     * On network loss, the current socket is closed so ensureSocket() recreates on next send.
     */
    private fun registerNetworkCallback() {
        if (networkCallback != null) return  // Already registered
        val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onLost(network: Network) {
                Log.w(TAG, "Network lost, closing socket for reconnection")
                socket?.close()
                socket = null
            }
            override fun onAvailable(network: Network) {
                Log.i(TAG, "Network available, will reconnect on next send")
            }
        }
        cm.registerDefaultNetworkCallback(cb)
        networkCallback = cb
    }

    /**
     * Unregister the network change callback.
     */
    private fun unregisterNetworkCallback() {
        networkCallback?.let {
            try {
                val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
                cm.unregisterNetworkCallback(it)
            } catch (e: Exception) {
                Log.w(TAG, "Error unregistering network callback: ${e.message}")
            }
        }
        networkCallback = null
    }

    /**
     * Start in idle mode: create socket, listen for ACKs, send heartbeats.
     * No GPS, no tracking. Used when auto-starting on boot.
     */
    private fun startInIdleMode() {
        Log.i(TAG, "Starting in idle mode")

        // Use a default idle interval if server hasn't told us one yet (e.g. fresh boot).
        // The real interval will be learned from the first server ACK.
        if (idleIntervalMs <= 0) {
            idleIntervalMs = 15000L  // 15 seconds default until server ACK overrides
            Log.i(TAG, "No idle interval from server yet, using default ${idleIntervalMs}ms")
        }

        // Register for shutdown events and network changes
        registerShutdownReceiver()
        registerNetworkCallback()

        // Enter idle mode (handles wake lock, notification, heartbeat loop)
        // Socket is created lazily by ensureSocket() on first send
        enterIdleMode()
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

        playTrackingTones(ascending = true)

        // Mark tracking as active so shutdown handler sends stop packet
        getPrefs().edit().putBoolean("tracking_active", true).apply()

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

        Log.d(TAG, "Starting tracking to $serverHost:$serverPort as $sailorId")

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

        // Ensure socket exists (creates if needed, reuses if healthy from idle mode)
        serviceScope.launch { ensureSocket() }

        // Register for network changes so socket is recreated on WiFi/cellular transitions
        registerNetworkCallback()

        // Start location updates using native LocationManager (works during Direct Boot)
        val intervalMs = 1000L

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

                    // Track GPS fix state for status line
                    if (usedInFix == 0) {
                        hasGpsFix.set(false)
                    }
                }
            }
            try {
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
                    locationManager.registerGnssStatusCallback(mainExecutor, gnssStatusCallback!!)
                } else {
                    @Suppress("DEPRECATION")
                    locationManager.registerGnssStatusCallback(gnssStatusCallback!!, android.os.Handler(mainLooper))
                }
                Log.i(TAG, "Registered GNSS status callback for satellite count")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to register GNSS status callback", e)
            }

            updateNotification("Tracking active")

            // Start keepalive loop - ensures a packet is sent every 10s while tracking.
            // If a position packet was sent recently, this is a no-op.
            // Otherwise sends a GPS-wait heartbeat so the server knows we're alive.
            lastPositionSendTime.set(0)
            keepaliveJob = serviceScope.launch {
                while (isRunning.get()) {
                    val elapsed = System.currentTimeMillis() - lastPositionSendTime.get()
                    if (elapsed >= LOCATION_INTERVAL_MS) {
                        sendGpsWaitPacket()
                    }
                    delay(LOCATION_INTERVAL_MS)
                }
            }

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

        // Include last known position so server processes via the normal path
        // (packets with lat=0/lon=0 hit a no-GPS branch that may skip stopped flag)
        val prefs = getPrefs()
        val lastLat = prefs.getFloat("last_lat", 0f)
        val lastLon = prefs.getFloat("last_lon", 0f)

        val packet = JSONObject().apply {
            put("id", sailorId)
            put("eid", eventId)
            put("sq", seq)
            put("ts", System.currentTimeMillis() / 1000)
            put("stopped", true)  // Deliberate stop
            put("ver", BuildConfig.VERSION_STRING)
            if (lastLat != 0f && lastLon != 0f) {
                put("lat", lastLat.toDouble())
                put("lon", lastLon.toDouble())
            }
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

        // Stop keepalive heartbeat
        keepaliveJob?.cancel()
        keepaliveJob = null

        // Stop tracker beep timer
        beepHandler.removeCallbacks(beepRunnable)
        toneGenerator?.release()
        toneGenerator = null

        // Stop assist alarm
        assistAlarmHandler.removeCallbacks(assistAlarmRunnable)

        // Stop notification update timer
        notificationHandler.removeCallbacks(notificationUpdateRunnable)

        // Unregister network callback and shutdown receiver
        unregisterNetworkCallback()
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

        // Cancel ACK listener and close socket
        ackListenerJob?.cancel()
        ackListenerJob = null
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

        playTrackingTones(ascending = false)

        serviceScope.launch {
            // If idle mode is supported, skip the stop packet — enterIdleMode() will
            // send an idle heartbeat immediately, avoiding a brief STOPPED flash in the UI
            if (idleIntervalMs > 0) {
                clearLastPosition()
                withContext(Dispatchers.Main) {
                    enterIdleMode()
                    statusListener?.onIdleEntered()
                }
            } else {
                sendStopPacket()
                clearLastPosition()
                withContext(Dispatchers.Main) {
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

        // Stop keepalive heartbeat
        keepaliveJob?.cancel()
        keepaliveJob = null

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
        // Send first idle packet immediately so server/WebUI updates right away
        idleJob = serviceScope.launch {
            sendIdlePacket()
            while (isIdleMode.get() && idleIntervalMs > 0) {
                delay(idleIntervalMs)
                sendIdlePacket()
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

        // Cancel ACK listener
        ackListenerJob?.cancel()
        ackListenerJob = null

        // Send stop packet on a background thread (network I/O not allowed on main thread),
        // then close socket and stop service
        Thread {
            sendStopPacketSync()

            // Close socket and stop service
            isRunning.set(false)
            socket?.close()
            socket = null

            Handler(Looper.getMainLooper()).post {
                // Unregister network callback and shutdown receiver
                unregisterNetworkCallback()
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
        }.start()
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
            put("chg", batteryManager.isCharging)
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

        val sock = ensureSocket()
        if (sock == null) {
            Log.w(TAG, "No socket available for idle heartbeat, will retry next interval")
            return
        }
        try {
            val dgram = DatagramPacket(data, data.size, address, serverPort)
            sock.send(dgram)
            Log.d(TAG, "Sent idle heartbeat seq=$seq bat=$batteryPercent%")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to send idle heartbeat, closing socket for reconnection", e)
            socket?.close()
            socket = null
        }
    }

    /**
     * Send a GPS-wait heartbeat packet (tracking active but no GPS fix yet).
     * Sends a normal packet with nsats=0 and no lat/lon so the server knows
     * we're alive and can send commands back via ACK.
     */
    private suspend fun sendGpsWaitPacket() {
        val seq = sequenceNumber.incrementAndGet()
        val currentPassword = getCurrentPassword()
        val eventId = getCurrentEventId()

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
            put("spd", 0)
            put("hdg", 0)
            put("ast", assistRequested.get())
            put("bat", batteryPercent)
            put("chg", batteryManager.isCharging)
            put("sig", signalLevel)
            put("nsats", 0)
            put("role", role)
            put("ver", BuildConfig.VERSION_STRING)
            put("os", "Android ${android.os.Build.VERSION.RELEASE}")
            if (currentPassword.isNotEmpty()) {
                put("pwd", currentPassword)
            }
        }

        val data = packet.toString().toByteArray(Charsets.UTF_8)
        val address = getServerAddress() ?: return

        val sock = ensureSocket()
        if (sock == null) {
            Log.w(TAG, "No socket available for GPS-wait heartbeat, will retry next interval")
            return
        }
        try {
            val dgram = DatagramPacket(data, data.size, address, serverPort)
            sock.send(dgram)
            Log.d(TAG, "Sent GPS-wait heartbeat seq=$seq bat=$batteryPercent%")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to send GPS-wait heartbeat, closing socket for reconnection", e)
            socket?.close()
            socket = null
        }
    }

    fun isIdle(): Boolean = isIdleMode.get()

    /**
     * Start tracking from idle mode. Called when user presses Start while service is idle.
     */
    fun startTrackingFromIdle() {
        startTracking()
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
        lastPositionSendTime.set(System.currentTimeMillis())
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
        lastPositionSendTime.set(System.currentTimeMillis())

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
        // Don't start if already running
        if (ackListenerJob?.isActive == true) return

        ackListenerJob = serviceScope.launch {
            val buffer = ByteArray(256)

            while (isRunning.get() || isIdleMode.get()) {
                try {
                    val dgram = DatagramPacket(buffer, buffer.size)
                    socket?.receive(dgram)

                    val response = String(dgram.data, 0, dgram.length, Charsets.UTF_8)
                    val ack = JSONObject(response)
                    val ackSeq = ack.optInt("ack", -1)

                    // Handle proactive commands (ack==0, sent proactively by server)
                    val isProactive = ack.optBoolean("proactive", false)
                    if (isProactive) {
                        val cmd = ack.optString("cmd", "")
                        if (cmd.isNotEmpty()) {
                            Log.w(TAG, "Received proactive $cmd command from server")
                            handleRemoteCommand(cmd)
                        }
                        continue
                    }

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
                            // If idle mode is active and server sends idle=0, shut down
                            if (isIdleMode.get() && interval == 0) {
                                Log.w(TAG, "Server sent idle=0 while in idle mode, shutting down")
                                Handler(Looper.getMainLooper()).post {
                                    exitIdleMode()
                                    statusListener?.onRemoteShutdown()
                                }
                            }
                        }

                        // Check for remote commands in normal ACK
                        val cmd = ack.optString("cmd", "")
                        if (cmd.isNotEmpty()) {
                            handleRemoteCommand(cmd)
                        }

                        Log.d(TAG, "Received ACK for seq=$ackSeq")
                    }
                } catch (e: java.net.SocketTimeoutException) {
                    // Normal timeout, continue
                } catch (e: Exception) {
                    if (isRunning.get() || isIdleMode.get()) {
                        Log.e(TAG, "ACK listener error", e)
                    }
                    // Socket error — close so ensureSocket() recreates on next send
                    if (e is java.net.SocketException) {
                        socket?.close()
                        socket = null
                        break  // Exit loop; ensureSocket() will restart listener on next send
                    }
                }
            }
        }
    }
    
    // Public methods for UI
    
    fun requestAssist(enabled: Boolean) {
        assistRequested.set(enabled)
        Log.d(TAG, "Assist ${if (enabled) "ENABLED" else "disabled"}")

        // Play feedback tones
        playAssistTones(ascending = enabled)

        // Manage the repeating alarm (tones every 5s while assist is active)
        if (enabled) {
            assistAlarmHandler.postDelayed(assistAlarmRunnable, 5000L)
        } else {
            assistAlarmHandler.removeCallbacks(assistAlarmRunnable)
        }

        // Send immediate position update so server/live view updates quickly
        lastLocation?.let { sendPosition(it) }
    }
    
    fun getLastLocation(): Location? = lastLocation
    
    /**
     * Handle a remote command from the server (from normal ACK or proactive push).
     */
    private fun handleRemoteCommand(cmd: String) {
        if (cmd == "stop") {
            Log.w(TAG, "Received remote STOP command from server")
            requestGracefulStop {
                Handler(Looper.getMainLooper()).post {
                    statusListener?.onRemoteStop()
                }
            }
        } else if (cmd == "cancel_assist") {
            Log.w(TAG, "Received remote CANCEL ASSIST command from server")
            requestAssist(false)
            Handler(Looper.getMainLooper()).post {
                statusListener?.onRemoteCancelAssist()
            }
        } else if (cmd == "start") {
            if (isIdleMode.get()) {
                Log.w(TAG, "Received remote START command from server")
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
    }

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
