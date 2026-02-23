package nz.co.tracker.windsurfer

import android.app.*
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Location
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.media.ToneGenerator
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.BatteryManager
import android.os.Binder
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.speech.tts.TextToSpeech
import android.telephony.TelephonyManager
import android.util.Log
import android.graphics.drawable.Icon
import androidx.core.graphics.drawable.IconCompat
import android.graphics.Color
import java.util.Locale
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LifecycleService
import androidx.wear.ongoing.OngoingActivity
import androidx.wear.ongoing.Status
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
        private const val CHANNEL_ID = "tracker_channel_v2"

        const val DEFAULT_SERVER_HOST = "wstracker.org"
        const val DEFAULT_SERVER_PORT = 41234
        const val LOCATION_INTERVAL_MS = 10000L  // 10 seconds
        const val UDP_RETRY_COUNT = 3
        const val UDP_RETRY_DELAY_MS = 1500L
        const val ACK_TIMEOUT_MS = 2000L
        const val MAX_ACCURACY_METERS = 100.0f

        // Notification action for race timer
        const val ACTION_START_TIMER = "nz.co.tracker.windsurfer.START_TIMER"
        const val ACTION_RESET_TIMER = "nz.co.tracker.windsurfer.RESET_TIMER"
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
    private var totalDistance: Float = 0f  // Total distance in meters

    // UDP
    private var socket: DatagramSocket? = null
    private var serverHost: String = DEFAULT_SERVER_HOST
    private var serverPort: Int = DEFAULT_SERVER_PORT
    private var sailorId: String = ""
    private var role: String = "sailor"
    private var password: String = ""
    private var eventId: Int = 2  // Event ID for multi-event support
    private var heartRateEnabled: Boolean = false
    private var raceTimerEnabled: Boolean = false
    private var raceTimerMinutes: Int = 5
    private var raceTimerTapGForce: Int = 3  // 2-9g, default 3g

    // Broadcast receiver for notification timer actions
    private val timerActionReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                ACTION_START_TIMER -> {
                    // Only start if in waiting state (-1), not if expired (0)
                    if (!countdownRunning && countdownSeconds < 0) {
                        startCountdown(raceTimerMinutes)
                        updateNotificationWithTimer()
                    }
                }
                ACTION_RESET_TIMER -> {
                    resetCountdown()
                    updateNotificationWithTimer()
                }
            }
        }
    }

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
    private val assistButtonEnabled = AtomicBoolean(true)  // Whether assist button should be shown (from server)
    private val sequenceNumber = AtomicInteger(0)
    private val lastAckTime = AtomicLong(0)
    private val hasGpsFix = AtomicBoolean(false)
    private val hasFirstAck = AtomicBoolean(false)
    private val hasAuthFailure = AtomicBoolean(false)
    private var currentEventName: String = ""

    // Idle mode state
    private val isIdleMode = AtomicBoolean(false)
    private var idleIntervalMs: Long = 0
    private var idleJob: Job? = null
    private var idleWakeLock: PowerManager.WakeLock? = null
    // Sliding window for ACK rate calculation (last 20 messages)
    private val ackWindow = java.util.concurrent.ConcurrentLinkedDeque<Boolean>()
    private val ACK_WINDOW_SIZE = 20
    // Track sequences that have been recorded in the window (to avoid double-counting)
    private val recordedSeqs = java.util.concurrent.ConcurrentHashMap.newKeySet<Int>()

    // Track acknowledged sequence numbers to stop retransmissions
    private val acknowledgedSeqs = java.util.concurrent.ConcurrentHashMap.newKeySet<Int>()
    private var pulseToggle = false

    // Tracker beep - plays once per minute to remind user tracker is running
    private var toneGenerator: ToneGenerator? = null
    private val beepHandler = Handler(Looper.getMainLooper())
    private var trackerBeepEnabled: Boolean = true
    private val beepRunnable = object : Runnable {
        override fun run() {
            Log.d(TAG, "Beep timer fired: isRunning=${isRunning.get()}, beepEnabled=$trackerBeepEnabled")
            if (isRunning.get() && trackerBeepEnabled) {
                playTrackerBeep()
            }
            if (isRunning.get()) {
                beepHandler.postDelayed(this, 60000L)  // Every 60 seconds
            }
        }
    }

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

    // Race countdown timer
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var countdownSeconds = -1  // -1 = waiting to start, 0 = expired, > 0 = running
    private var countdownRunning = false
    private var countdownStartMinutes = 5
    private val countdownHandler = Handler(Looper.getMainLooper())
    private var countdownTargetTime = 0L  // SystemClock.elapsedRealtime() when countdown reaches 0
    private val TTS_LATENCY_MS = 250L  // Announce early to compensate for TTS delay
    private var lastAnnouncedSecond = -1  // Track to prevent duplicate announcements

    private val countdownRunnable = object : Runnable {
        override fun run() {
            if (!countdownRunning) return

            val now = android.os.SystemClock.elapsedRealtime()
            val msRemaining = countdownTargetTime - now

            if (msRemaining <= -500) {
                // Past the start time
                countdownRunning = false
                statusListener?.onCountdownFinished()
                return
            }

            // Calculate seconds for display (round to nearest)
            val displaySeconds = maxOf(0, ((msRemaining + 500) / 1000).toInt())
            if (displaySeconds != countdownSeconds) {
                countdownSeconds = displaySeconds
                statusListener?.onCountdownTick(countdownSeconds)
            }

            // Announce early to compensate for TTS latency
            // Calculate which second we should announce now (accounting for latency)
            val adjustedMs = msRemaining - TTS_LATENCY_MS
            val announceSecond = ((adjustedMs + 999) / 1000).toInt()  // Ceiling

            if (announceSecond != lastAnnouncedSecond && announceSecond >= 0) {
                if (announceSecond == 0) {
                    announceStart()
                } else {
                    announceCountdownIfNeeded(announceSecond)
                }
                lastAnnouncedSecond = announceSecond
            }

            // Run at high frequency for accurate timing
            countdownHandler.postDelayed(this, 50L)
        }
    }

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

    // Tap detection for race timer (acceleration magnitude spike above gravity)
    private var accelerometer: Sensor? = null
    private var lastTapTime: Long = 0
    private val GRAVITY = 9.81f
    private val TAP_THRESHOLD: Float
        get() = raceTimerTapGForce * GRAVITY  // Convert g-force to m/s²
    private val TAP_COOLDOWN_MS = 1000L  // Prevent multiple triggers

    private val tapListener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            if (event.sensor.type != Sensor.TYPE_ACCELEROMETER) return
            if (!raceTimerEnabled) return

            val x = event.values[0]
            val y = event.values[1]
            val z = event.values[2]

            // Calculate magnitude and subtract gravity
            val magnitude = Math.sqrt((x * x + y * y + z * z).toDouble()).toFloat()
            val accelAboveGravity = Math.abs(magnitude - GRAVITY)

            // Detect tap (acceleration spike above gravity)
            if (accelAboveGravity > TAP_THRESHOLD) {
                val now = System.currentTimeMillis()
                if (now - lastTapTime > TAP_COOLDOWN_MS) {
                    lastTapTime = now
                    // State machine:
                    // - Running (countdownRunning=true): tap resets to waiting
                    // - Expired (countdownSeconds=0): tap resets to waiting
                    // - Waiting (countdownSeconds=-1): tap starts countdown
                    if (countdownRunning) {
                        resetCountdown()  // Running → Waiting
                    } else if (countdownSeconds == 0) {
                        resetCountdown()  // Expired → Waiting
                    } else {
                        startCountdown(raceTimerMinutes)  // Waiting → Running
                    }
                    updateNotificationWithTimer()
                }
            }
        }
        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
    }

    // GPS-wait heartbeat job (sends packets while waiting for GPS fix)
    private var gpsWaitJob: Job? = null

    // Keepalive job (flushes buffered positions and sends heartbeats during tracking)
    private var keepaliveJob: Job? = null

    // Coroutines
    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    // Tracking start time for ongoing activity text
    private var trackingStartRealtimeMs: Long = 0L

    // Listener for UI updates
    var statusListener: StatusListener? = null

    interface StatusListener {
        fun onLocationUpdate(location: Location, totalDistanceMeters: Float)
        fun onAckReceived(seq: Int)
        fun onPacketSent(seq: Int)
        fun onConnectionStatus(ackRate: Float)
        fun onEventName(name: String)
        fun onError(message: String)
        fun onStatusLine(status: String)  // GPS wait, connecting..., auth failure, or event name
        fun onAssistEnabled(enabled: Boolean)  // Whether assist button should be shown
        fun onRemoteStop()  // Server sent remote stop command
        fun onRemoteCancelAssist()  // Server sent remote cancel assist command
        fun onIdleEntered()  // Entered idle mode after user stop
        fun onRemoteStart()  // Server sent start command (from idle mode)
        fun onRemoteShutdown()  // Server sent shutdown command (from idle mode)
        fun onCountdownTick(secondsRemaining: Int)  // Race timer countdown
        fun onCountdownFinished()  // Race timer reached zero naturally
        fun onCountdownReset()  // Race timer manually reset by user
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
     * Play tracker beep: bip-bip if ACK received in last minute, bip-boop if not.
     * Uses vibration since watch speakers are often muted/unavailable.
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
     * Play assist tones: triple ascending (low→mid→high) or descending (high→mid→low).
     * Uses STREAM_ALARM at MAX_VOLUME so tones are audible even with media muted.
     */
    private fun playAssistTones(ascending: Boolean) {
        try {
            val tg = ToneGenerator(AudioManager.STREAM_ALARM, ToneGenerator.MAX_VOLUME)
            val tones = if (ascending) {
                intArrayOf(ToneGenerator.TONE_DTMF_1, ToneGenerator.TONE_DTMF_5, ToneGenerator.TONE_DTMF_9)
            } else {
                intArrayOf(ToneGenerator.TONE_DTMF_9, ToneGenerator.TONE_DTMF_5, ToneGenerator.TONE_DTMF_1)
            }
            val h = Handler(Looper.getMainLooper())
            tg.startTone(tones[0], 150)
            h.postDelayed({ tg.startTone(tones[1], 150) }, 200)
            h.postDelayed({ tg.startTone(tones[2], 150) }, 400)
            h.postDelayed({ tg.release() }, 600)
        } catch (e: Exception) {
            Log.w(TAG, "Failed to play assist tones: ${e.message}")
        }
    }

    /**
     * Play tracking start/stop tones: double ascending (low→high) or descending (high→low).
     * Uses STREAM_ALARM at MAX_VOLUME.
     */
    private fun playTrackingTones(ascending: Boolean) {
        Thread {
            try {
                val sampleRate = 44100
                val toneSamples = sampleRate * 250 / 1000   // 250ms per tone
                val gapSamples = sampleRate * 80 / 1000     // 80ms gap
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
                                Math.sin(2 * Math.PI * freq.second * t)) * 0.49 * Short.MAX_VALUE
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

                // Force alarm stream to max volume
                val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
                val maxVol = am.getStreamMaxVolume(AudioManager.STREAM_ALARM)
                am.setStreamVolume(AudioManager.STREAM_ALARM, maxVol, 0)

                track.write(buffer, 0, buffer.size)
                track.play()
                Thread.sleep((250 * 2 + 80 + 50).toLong())
                track.stop()
                track.release()
            } catch (e: Exception) {
                Log.w(TAG, "Failed to play tracking tones: ${e.message}")
            }
        }.start()
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

        // Initialize TTS for race countdown
        tts = TextToSpeech(this) { status ->
            ttsReady = status == TextToSpeech.SUCCESS
            if (ttsReady) {
                tts?.language = Locale.US
                Log.d(TAG, "TTS initialized successfully")
            } else {
                Log.w(TAG, "TTS initialization failed")
            }
        }

        // Register broadcast receiver for notification timer actions
        val filter = IntentFilter().apply {
            addAction(ACTION_START_TIMER)
            addAction(ACTION_RESET_TIMER)
        }
        registerReceiver(timerActionReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)

        if (intent != null) {
            // Normal start from MainActivity - read config from intent extras
            serverHost = intent.getStringExtra("server_host") ?: DEFAULT_SERVER_HOST
            serverPort = intent.getIntExtra("server_port", DEFAULT_SERVER_PORT)
            sailorId = intent.getStringExtra("sailor_id") ?: ""
            role = intent.getStringExtra("role") ?: "sailor"
            password = intent.getStringExtra("password") ?: ""
            eventId = intent.getIntExtra("event_id", 2)
            heartRateEnabled = intent.getBooleanExtra("heart_rate_enabled", false)
            trackerBeepEnabled = intent.getBooleanExtra("tracker_beep", true)
            raceTimerEnabled = intent.getBooleanExtra("race_timer_enabled", false)
            raceTimerMinutes = intent.getIntExtra("race_timer_minutes", 5)
            raceTimerTapGForce = intent.getIntExtra("race_timer_tap_g_force", 3).coerceIn(2, 9)
            Log.d(TAG, "Race timer settings: enabled=$raceTimerEnabled, minutes=$raceTimerMinutes, tapGForce=${raceTimerTapGForce}g (threshold=${TAP_THRESHOLD}m/s²)")
            positionBuffer.clear()
            totalDistance = 0f
            previousLocation = null

            // Persist config for START_STICKY restarts (system may kill and restart service
            // with null intent, so we need saved config to avoid sending bad packets)
            saveServiceConfig()
        } else {
            // START_STICKY restart - system killed and restarted service with null intent.
            // Reload config from SharedPreferences.
            if (!loadServiceConfig()) {
                Log.w(TAG, "No saved config for START_STICKY restart, stopping service")
                stopSelf()
                return START_STICKY
            }
            Log.i(TAG, "Restored config from SharedPreferences: id=$sailorId event=$eventId")
            positionBuffer.clear()
            totalDistance = 0f
            previousLocation = null
        }

        // Don't restart tracking if in idle mode (service is alive for heartbeats)
        if (!isIdleMode.get()) {
            startForegroundService()
            startTracking()
        }

        return START_STICKY
    }

    /**
     * Save service config to device-protected SharedPreferences for START_STICKY restarts.
     */
    private fun saveServiceConfig() {
        val prefs = createDeviceProtectedStorageContext()
            .getSharedPreferences("tracker_service_prefs", Context.MODE_PRIVATE)
        prefs.edit().apply {
            putString("server_host", serverHost)
            putInt("server_port", serverPort)
            putString("sailor_id", sailorId)
            putString("role", role)
            putString("password", password)
            putInt("event_id", eventId)
            putBoolean("heart_rate_enabled", heartRateEnabled)
            putBoolean("tracker_beep", trackerBeepEnabled)
            putBoolean("race_timer_enabled", raceTimerEnabled)
            putInt("race_timer_minutes", raceTimerMinutes)
            putInt("race_timer_tap_g_force", raceTimerTapGForce)
            apply()
        }
        Log.d(TAG, "Saved service config to SharedPreferences")
    }

    /**
     * Load service config from device-protected SharedPreferences.
     * Returns true if valid config was loaded, false if no saved config exists.
     */
    private fun loadServiceConfig(): Boolean {
        val prefs = createDeviceProtectedStorageContext()
            .getSharedPreferences("tracker_service_prefs", Context.MODE_PRIVATE)
        val savedId = prefs.getString("sailor_id", "") ?: ""
        val savedPwd = prefs.getString("password", "") ?: ""
        if (savedId.isEmpty() || savedPwd.isEmpty()) {
            return false
        }
        serverHost = prefs.getString("server_host", DEFAULT_SERVER_HOST) ?: DEFAULT_SERVER_HOST
        serverPort = prefs.getInt("server_port", DEFAULT_SERVER_PORT)
        sailorId = savedId
        role = prefs.getString("role", "sailor") ?: "sailor"
        password = savedPwd
        eventId = prefs.getInt("event_id", 2)
        heartRateEnabled = prefs.getBoolean("heart_rate_enabled", false)
        trackerBeepEnabled = prefs.getBoolean("tracker_beep", true)
        raceTimerEnabled = prefs.getBoolean("race_timer_enabled", false)
        raceTimerMinutes = prefs.getInt("race_timer_minutes", 5)
        raceTimerTapGForce = prefs.getInt("race_timer_tap_g_force", 3).coerceIn(2, 9)
        return true
    }

    override fun onBind(intent: Intent): IBinder {
        super.onBind(intent)
        return binder
    }

    override fun onDestroy() {
        super.onDestroy()
        Log.i(TAG, "onDestroy called, isRunning=${isRunning.get()}, isIdle=${isIdleMode.get()}")
        if (isRunning.get() || isIdleMode.get()) {
            // Service being killed while tracking/idle — alert the user
            playTrackingTones(ascending = false)
        }
        stopTracking()

        // Clean up idle mode if active
        if (isIdleMode.get()) {
            idleJob?.cancel()
            idleJob = null
            idleWakeLock?.let { if (it.isHeld) it.release() }
            idleWakeLock = null
            isIdleMode.set(false)
        }

        // Unregister broadcast receiver
        try {
            unregisterReceiver(timerActionReceiver)
        } catch (e: Exception) {
            Log.w(TAG, "Failed to unregister timer receiver", e)
        }

        // Shutdown TTS
        tts?.shutdown()
        tts = null

        // Stop countdown timer
        countdownHandler.removeCallbacks(countdownRunnable)
        countdownRunning = false

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
        val notification = buildNotification("Starting tracker...", showTimerAction = raceTimerEnabled)
        startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        // Ensure the system sees the OngoingActivity payload after foreground promotion
        updateNotification(currentStatusText())
    }

    private var ongoingActivity: OngoingActivity? = null

    private enum class ConnectionState { OK, WARN, ERROR }

    private fun getConnectionState(): ConnectionState {
        val diff = System.currentTimeMillis() - lastAckTime.get()
        val sinceStart = System.currentTimeMillis() - trackingStartRealtimeMs
        if (sinceStart in 0..20_000) {
            return ConnectionState.OK
        }
        return when {
            diff in 1..30_000 -> ConnectionState.OK
            diff in 30_001..60_000 -> ConnectionState.WARN
            else -> ConnectionState.ERROR
        }
    }

    private fun getStateIconRes(): Int {
        if (isIdleMode.get()) {
            return R.drawable.ic_notification_idle
        }
        val diff = System.currentTimeMillis() - lastAckTime.get()
        return if (diff in 1..30_000) {
            R.drawable.ic_notification_ok
        } else {
            R.drawable.ic_notification_error
        }
    }

    private fun createMainPendingIntent(): PendingIntent {
        val intent = Intent(this, nz.co.tracker.windsurfer.presentation.MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        return PendingIntent.getActivity(
            this, 1001, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun applyOngoingActivity(notificationBuilder: NotificationCompat.Builder, statusText: String) {
        val pendingIntent = createMainPendingIntent()

        val status = Status.Builder().addTemplate(statusText).build()
        val stateIconRes = getStateIconRes()
        ongoingActivity = OngoingActivity.Builder(this, NOTIFICATION_ID, notificationBuilder)
            .setAnimatedIcon(stateIconRes)
            .setStaticIcon(stateIconRes)
            .setTouchIntent(pendingIntent)
            .setStatus(status)
            .build()

        ongoingActivity?.apply(this)
        Log.d(TAG, "Applied ongoing activity with status='$statusText'")
    }

    private fun clearOngoingActivity() {
        ongoingActivity = null
    }

    private fun buildNotificationBuilder(text: String, showTimerAction: Boolean = false): NotificationCompat.Builder {
        val pendingIntent = createMainPendingIntent()

        val stateIconRes = getStateIconRes()

        val builder = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Windsurfer Tracker")
            .setContentText(text)
            .setSmallIcon(stateIconRes)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setCategory(NotificationCompat.CATEGORY_WORKOUT)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text).setBigContentTitle("Windsurfer Tracker"))

        // Add timer action button if race timer is enabled
        if (raceTimerEnabled && showTimerAction) {
            if (countdownRunning) {
                // Show RESET action when timer is running
                val resetIntent = Intent(ACTION_RESET_TIMER).setPackage(packageName)
                val resetPendingIntent = PendingIntent.getBroadcast(
                    this, 1, resetIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                )
                builder.addAction(
                    android.R.drawable.ic_menu_close_clear_cancel,
                    "RESET",
                    resetPendingIntent
                )
            } else {
                // Show START TIMER action when timer is not running
                val startIntent = Intent(ACTION_START_TIMER).setPackage(packageName)
                val startPendingIntent = PendingIntent.getBroadcast(
                    this, 2, startIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                )
                builder.addAction(
                    android.R.drawable.ic_media_play,
                    "▶ START ${raceTimerMinutes}MIN",
                    startPendingIntent
                )
            }
        }

        return builder
    }

    private fun buildNotification(text: String, showTimerAction: Boolean = false): Notification {
        val builder = buildNotificationBuilder(text, showTimerAction)
        applyOngoingActivity(builder, text)
        return builder.build()
    }

    private fun getStatusColor(): Int {
        val now = System.currentTimeMillis()
        val diff = now - lastAckTime.get()
        return when {
            diff in 1..30_000 -> Color.parseColor("#2ECC71") // green: healthy ACKs
            diff in 30_001..60_000 -> Color.parseColor("#F5A623") // orange: stale
            else -> Color.parseColor("#E53935") // red: no ACKs recently
        }
    }

    /**
     * Update the notification with timer action button
     */
    private fun updateNotificationWithTimer() {
        val text = if (countdownRunning) {
            val mins = countdownSeconds / 60
            val secs = countdownSeconds % 60
            "Timer: $mins:${String.format("%02d", secs)}"
        } else {
            "Tracking active"
        }
        val notification = buildNotification(text, showTimerAction = true)
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, notification)
    }

    private fun updateNotification(text: String) {
        val notification = buildNotification(text)
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, notification)
    }

    private fun currentStatusText(): String {
        val elapsedMs = if (trackingStartRealtimeMs > 0) {
            android.os.SystemClock.elapsedRealtime() - trackingStartRealtimeMs
        } else {
            0L
        }
        val totalSeconds = elapsedMs / 1000
        val minutes = totalSeconds / 60
        val seconds = totalSeconds % 60
        return "Tracking ${minutes}m ${String.format("%02d", seconds)}s"
    }

    private fun setupLocationCallback() {
        locationCallback = object : LocationCallback() {
            override fun onLocationResult(result: LocationResult) {
                result.lastLocation?.let { location ->
                    // Skip GPS updates if in idle mode (GPS should be stopped but
                    // fused location provider may deliver queued updates)
                    if (isIdleMode.get()) return

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

                    // Poor accuracy: send position so server knows approximate location,
                    // but don't update tracking state (speed/distance calculations)
                    if (MAX_ACCURACY_METERS > 0 && location.accuracy > MAX_ACCURACY_METERS) {
                        Log.d(TAG, "Poor accuracy location: ${location.accuracy}m > ${MAX_ACCURACY_METERS}m, sending as low-GPS")
                        val ts = System.currentTimeMillis() / 1000
                        val speedKnots = if (location.hasSpeed() && location.speed > 0) {
                            (location.speed * 1.94384 * 10).toInt() / 10.0
                        } else 0.0
                        positionBuffer.add(BufferedPosition(ts, location.latitude, location.longitude, speedKnots))
                        lastBufferedLocation = location
                        if (positionBuffer.size >= 10 ||
                            (positionBuffer.size > 0 && ts - positionBuffer.first().ts >= 10)) {
                            sendPositionArray()
                        }
                        return
                    }

                    // Calculate distance from previous location
                    previousLocation?.let { prevLoc ->
                        val distanceResult = FloatArray(1)
                        Location.distanceBetween(
                            prevLoc.latitude, prevLoc.longitude,
                            location.latitude, location.longitude,
                            distanceResult
                        )
                        val distance = distanceResult[0]
                        // Only add reasonable distances (filter GPS noise)
                        if (distance > 0.1f && distance < 500f) {
                            totalDistance += distance
                        }
                    }
                    previousLocation = location

                    lastLocation = location
                    statusListener?.onLocationUpdate(location, totalDistance)

                    // Mark GPS as ready and update status line
                    if (!hasGpsFix.getAndSet(true)) {
                        gpsWaitJob?.cancel()
                        gpsWaitJob = null
                        updateStatusLine()  // Show "connecting ..."
                    }

                    // Buffer position for batched sending (1Hz mode)
                    val ts = System.currentTimeMillis() / 1000
                    val speedKnots = if (location.hasSpeed() && location.speed > 0) {
                        (location.speed * 1.94384 * 10).toInt() / 10.0  // Round to 1 decimal
                    } else 0.0
                    positionBuffer.add(BufferedPosition(ts, location.latitude, location.longitude, speedKnots))
                    lastBufferedLocation = location

                    // Send every 10 positions or 10 seconds (whichever comes first)
                    if (positionBuffer.size >= 10 ||
                        (positionBuffer.size > 0 && ts - positionBuffer.first().ts >= 10)) {
                        sendPositionArray()
                    }

                    // Refresh ongoing activity text with elapsed tracking time
                    updateNotification(currentStatusText())
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

        // Clean up idle mode state if resuming from idle
        if (isIdleMode.getAndSet(false)) {
            Log.i(TAG, "Cleaning up idle state before starting tracking")
            idleJob?.cancel()
            idleJob = null
            idleWakeLock?.let { if (it.isHeld) it.release() }
            idleWakeLock = null
        }

        // Clear acknowledged sequences from previous session
        acknowledgedSeqs.clear()

        // Reset status tracking for new session
        hasGpsFix.set(false)
        hasFirstAck.set(false)
        hasAuthFailure.set(false)
        currentEventName = ""
        updateStatusLine()  // Show "GPS wait"
        trackingStartRealtimeMs = android.os.SystemClock.elapsedRealtime()

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

        // Start accelerometer for tap detection if race timer is enabled
        Log.d(TAG, "Race timer enabled: $raceTimerEnabled, minutes: $raceTimerMinutes")
        if (raceTimerEnabled) {
            if (sensorManager == null) {
                sensorManager = getSystemService(Context.SENSOR_SERVICE) as SensorManager
            }
            accelerometer = sensorManager?.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
            if (accelerometer != null) {
                val registered = sensorManager?.registerListener(tapListener, accelerometer, SensorManager.SENSOR_DELAY_FASTEST)
                Log.d(TAG, "Tap detection registered: $registered for race timer")
            } else {
                Log.w(TAG, "Accelerometer not available for tap detection")
            }
        }

        Log.d(TAG, "Starting tracking to $serverHost:$serverPort as $sailorId")

        // Play double ascending tone to indicate tracking started
        playTrackingTones(ascending = true)

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

        val intervalMs = 1000L
        val locationRequest = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, intervalMs)
            .setMinUpdateIntervalMillis(intervalMs / 2)
            .build()

        try {
            fusedLocationClient.requestLocationUpdates(
                locationRequest,
                locationCallback,
                Looper.getMainLooper()
            )
            updateNotification(currentStatusText())

            // Start GPS-wait heartbeat (sends packets with nsats=0 until GPS fix)
            gpsWaitJob = serviceScope.launch {
                while (isRunning.get() && !hasGpsFix.get()) {
                    sendGpsWaitPacket()
                    delay(LOCATION_INTERVAL_MS)
                }
            }

            // Start keepalive loop to flush buffered positions every 10s
            keepaliveJob = serviceScope.launch {
                while (isRunning.get()) {
                    delay(LOCATION_INTERVAL_MS)
                    if (positionBuffer.size > 0) {
                        val ts = System.currentTimeMillis() / 1000
                        if (ts - positionBuffer.first().ts >= 10) {
                            sendPositionArray()
                        }
                    }
                }
            }

            // Start tracker beep timer (first beep after 60 seconds)
            beepHandler.postDelayed(beepRunnable, 60000L)
        } catch (e: SecurityException) {
            Log.e(TAG, "Location permission denied", e)
        }
    }

    private fun stopTracking() {
        if (!isRunning.getAndSet(false)) return

        Log.d(TAG, "Stopping tracking")
        trackingStartRealtimeMs = 0L

        // Stop assist alarm if running
        assistAlarmHandler.removeCallbacks(assistAlarmRunnable)

        // Stop GPS-wait heartbeat and keepalive
        gpsWaitJob?.cancel()
        gpsWaitJob = null
        keepaliveJob?.cancel()
        keepaliveJob = null

        // Stop tracker beep timer
        beepHandler.removeCallbacks(beepRunnable)
        toneGenerator?.release()
        toneGenerator = null

        fusedLocationClient.removeLocationUpdates(locationCallback)

        // Release network request before closing socket
        releaseNetworkRequest()

        socket?.close()
        socket = null

        // Unregister heart rate sensor
        sensorManager?.unregisterListener(heartRateListener)
        lastHeartRate = -1

        // Unregister tap listener
        sensorManager?.unregisterListener(tapListener)
        accelerometer = null

        // Release wake lock
        wakeLock?.let {
            if (it.isHeld) {
                it.release()
                Log.d(TAG, "Wake lock released")
            }
        }
        wakeLock = null

        // Clear ongoing activity and remove the foreground notification
        clearOngoingActivity()
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
            put("os", "WearOS ${android.os.Build.VERSION.RELEASE}")
            if (password.isNotEmpty()) {
                put("pwd", password)
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
     * If idle mode is supported (idleIntervalMs > 0), enters idle mode instead of full stop.
     */
    fun requestGracefulStop(callback: (() -> Unit)? = null) {
        if (!isRunning.get()) {
            callback?.invoke()
            return
        }

        // Play double descending tone immediately for any deliberate stop
        playTrackingTones(ascending = false)

        serviceScope.launch {
            sendStopPacket()
            withContext(Dispatchers.Main) {
                if (idleIntervalMs > 0) {
                    enterIdleMode()
                    statusListener?.onIdleEntered()
                } else {
                    stopTracking()
                    callback?.invoke()
                }
            }
        }
    }

    /**
     * Send an idle heartbeat packet (no GPS data).
     */
    private suspend fun sendIdlePacket() {
        val seq = sequenceNumber.incrementAndGet()

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
            put("os", "WearOS ${android.os.Build.VERSION.RELEASE}")
            if (password.isNotEmpty()) {
                put("pwd", password)
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

    /**
     * Enter idle mode: stop GPS, keep socket, send heartbeats at server-configured interval.
     * Called when user stops tracking and server has idle support enabled.
     */
    private fun enterIdleMode() {
        if (isIdleMode.get()) return

        playTrackingTones(ascending = false)
        Log.i(TAG, "Entering idle mode (interval=${idleIntervalMs}ms)")
        isIdleMode.set(true)
        isRunning.set(false)

        // Stop GPS-wait heartbeat and keepalive
        gpsWaitJob?.cancel()
        gpsWaitJob = null
        keepaliveJob?.cancel()
        keepaliveJob = null

        // Stop GPS updates and clear position buffer
        fusedLocationClient.removeLocationUpdates(locationCallback)
        positionBuffer.clear()

        // Stop beep timer
        beepHandler.removeCallbacks(beepRunnable)
        toneGenerator?.release()
        toneGenerator = null

        // Unregister sensors
        sensorManager?.unregisterListener(heartRateListener)
        sensorManager?.unregisterListener(tapListener)
        lastHeartRate = -1

        // Release tracking wake lock
        wakeLock?.let {
            if (it.isHeld) it.release()
        }
        wakeLock = null

        // Keep socket and network request alive for ACK listener

        // Acquire idle wake lock
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        idleWakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "WindsurferTracker::IdleHeartbeat").apply {
            acquire()
        }

        // Update notification
        updateNotification("Idle - waiting for admin start")

        // Start idle heartbeat loop
        idleJob = serviceScope.launch {
            sendIdlePacket()
            while (isIdleMode.get() && idleIntervalMs > 0) {
                delay(idleIntervalMs)
                sendIdlePacket()
            }
        }
    }

    /**
     * Resume tracking from idle mode without creating a new Intent.
     * Clears idle state and starts GPS tracking using already-set fields.
     * Called when user taps screen while idle or via remote start command.
     */
    fun resumeFromIdle() {
        if (!isIdleMode.get()) return

        Log.i(TAG, "Resuming from idle mode")

        // Cancel idle heartbeat and release idle wake lock
        idleJob?.cancel()
        idleJob = null
        idleWakeLock?.let { if (it.isHeld) it.release() }
        idleWakeLock = null
        isIdleMode.set(false)

        // Update notification for tracking mode
        startForegroundService()

        // Start GPS tracking (startTracking checks isRunning, which is false in idle)
        startTracking()
    }

    /**
     * Exit idle mode: stop heartbeats, close socket, stop service.
     */
    fun exitIdleMode() {
        if (!isIdleMode.getAndSet(false)) return

        Log.i(TAG, "Exiting idle mode")

        // Clear saved config so START_STICKY doesn't restart after deliberate exit
        clearServiceConfig()

        // Cancel idle heartbeat and release wake lock
        idleJob?.cancel()
        idleJob = null
        idleWakeLock?.let {
            if (it.isHeld) it.release()
        }
        idleWakeLock = null

        // Release network request and close socket
        releaseNetworkRequest()
        socket?.close()
        socket = null

        // Clear ongoing activity and stop service
        clearOngoingActivity()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
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

    /**
     * Send a GPS-wait heartbeat packet (tracking active but no GPS fix yet).
     * Sends a normal packet with nsats=0 and no lat/lon so the server knows
     * we're alive and can send commands back via ACK.
     */
    private suspend fun sendGpsWaitPacket() {
        val seq = sequenceNumber.incrementAndGet()

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
            put("sig", signalLevel)
            put("nsats", 0)
            put("role", role)
            put("ver", BuildConfig.VERSION_STRING)
            put("os", "WearOS ${android.os.Build.VERSION.RELEASE}")
            if (password.isNotEmpty()) {
                put("pwd", password)
            }
        }

        val data = packet.toString().toByteArray(Charsets.UTF_8)
        val address = getServerAddress() ?: return

        try {
            val dgram = DatagramPacket(data, data.size, address, serverPort)
            socket?.send(dgram)
            Log.d(TAG, "Sent GPS-wait heartbeat seq=$seq bat=$batteryPercent%")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to send GPS-wait heartbeat", e)
        }
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

            while (isRunning.get() || isIdleMode.get()) {
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
                            if (errorType == "auth") {
                                hasAuthFailure.set(true)
                                updateStatusLine()  // Show "auth failure"
                            }
                            statusListener?.onError(errorMsg.ifEmpty { "Server error: $errorType" })
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
                        if (isRunning.get()) {
                            updateNotification(currentStatusText())
                        }

                        // Extract event name from ACK if present and update status
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
                            assistButtonEnabled.set(assistEnabled)
                            statusListener?.onAssistEnabled(assistEnabled)
                            // Clear local assist flag if server says assist is disabled
                            if (!assistEnabled && assistRequested.getAndSet(false)) {
                                Log.d(TAG, "Assist cleared by server (assist disabled for event)")
                                assistAlarmHandler.removeCallbacks(assistAlarmRunnable)
                                playAssistTones(ascending = false)
                            }
                        } else {
                            // Default to enabled if not specified
                            assistButtonEnabled.set(true)
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

                        // Check for remote commands
                        val cmd = ack.optString("cmd", "")
                        if (cmd == "stop") {
                            Log.w(TAG, "Received remote STOP command from server")
                            requestGracefulStop {
                                Handler(Looper.getMainLooper()).post {
                                    statusListener?.onRemoteStop()
                                }
                            }
                        } else if (cmd == "cancel_assist") {
                            Log.w(TAG, "Received remote CANCEL ASSIST command from server")
                            assistRequested.set(false)
                            assistAlarmHandler.removeCallbacks(assistAlarmRunnable)
                            playAssistTones(ascending = false)
                            Handler(Looper.getMainLooper()).post {
                                statusListener?.onRemoteCancelAssist()
                            }
                        } else if (cmd == "start") {
                            if (isIdleMode.get()) {
                                Log.w(TAG, "Received remote START command from server")
                                Handler(Looper.getMainLooper()).post {
                                    resumeFromIdle()
                                    statusListener?.onRemoteStart()
                                }
                            }
                        } else if (cmd == "shutdown") {
                            if (isIdleMode.get()) {
                                Log.w(TAG, "Received remote SHUTDOWN command from server")
                                Handler(Looper.getMainLooper()).post {
                                    exitIdleMode()
                                    statusListener?.onRemoteShutdown()
                                }
                            }
                        }

                        Log.d(TAG, "Received ACK for seq=$ackSeq${if (eventName.isNotEmpty()) " (event: $eventName)" else ""}")
                    }
                } catch (e: java.net.SocketTimeoutException) {
                    // Normal timeout, continue
                } catch (e: Exception) {
                    if (isRunning.get() || isIdleMode.get()) {
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

    fun isInIdleMode(): Boolean = isIdleMode.get()

    fun isAssistActive(): Boolean = assistRequested.get()

    fun replayStatusForUi() {
        // Push last known values to the UI listener after (re)binding
        lastLocation?.let { statusListener?.onLocationUpdate(it, totalDistance) }
        statusListener?.onConnectionStatus(getAckRate())
        if (currentEventName.isNotEmpty()) {
            statusListener?.onEventName(currentEventName)
        }
        statusListener?.onAssistEnabled(assistButtonEnabled.get())
        if (countdownRunning && countdownSeconds >= 0) {
            statusListener?.onCountdownTick(countdownSeconds)
        }
    }

    fun requestAssist(enabled: Boolean) {
        assistRequested.set(enabled)
        Log.d(TAG, "Assist ${if (enabled) "ENABLED" else "disabled"}")

        // Play assist tones and manage repeating alarm
        playAssistTones(ascending = enabled)
        if (enabled) {
            assistAlarmHandler.postDelayed(assistAlarmRunnable, 5000L)
        } else {
            assistAlarmHandler.removeCallbacks(assistAlarmRunnable)
        }

        // Send immediate position update when requesting assist
        if (enabled) {
            lastLocation?.let { sendPosition(it) }
        }
    }

    fun stopService() {
        // Clear saved config so START_STICKY doesn't restart with stale settings
        clearServiceConfig()
        stopTracking()
        clearOngoingActivity()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    /**
     * Clear saved service config so START_STICKY doesn't restart after deliberate stop.
     */
    private fun clearServiceConfig() {
        createDeviceProtectedStorageContext()
            .getSharedPreferences("tracker_service_prefs", Context.MODE_PRIVATE)
            .edit().clear().apply()
        Log.d(TAG, "Cleared saved service config")
    }

    // Race countdown timer methods

    private fun speak(text: String) {
        if (ttsReady) {
            tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, null)
        }
    }

    private fun announceCountdownIfNeeded(seconds: Int) {
        val vibrator = getSystemService(Context.VIBRATOR_SERVICE) as Vibrator

        when {
            seconds % 60 == 0 && seconds > 0 -> {
                // Each minute: "3 minutes", "2 minutes", "1 minute"
                val minutes = seconds / 60
                speak("$minutes minute${if (minutes > 1) "s" else ""}")
                vibrator.vibrate(VibrationEffect.createOneShot(200, VibrationEffect.DEFAULT_AMPLITUDE))
            }
            seconds == 30 -> {
                speak("30 seconds")
                vibrator.vibrate(VibrationEffect.createOneShot(200, VibrationEffect.DEFAULT_AMPLITUDE))
            }
            seconds == 20 -> {
                speak("20 seconds")
                vibrator.vibrate(VibrationEffect.createOneShot(200, VibrationEffect.DEFAULT_AMPLITUDE))
            }
            seconds <= 10 && seconds > 0 -> {
                // Final 10 seconds: "10", "9", ... "1"
                speak("$seconds")
                vibrator.vibrate(VibrationEffect.createOneShot(100, VibrationEffect.DEFAULT_AMPLITUDE))
            }
        }
    }

    private fun announceStart() {
        speak("Start!")
        val vibrator = getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
        // Triple buzz for start
        vibrator.vibrate(VibrationEffect.createWaveform(longArrayOf(0, 200, 100, 200, 100, 200), -1))
    }

    /**
     * Start the race countdown timer.
     * @param minutes Duration in minutes (1-9)
     */
    fun startCountdown(minutes: Int) {
        countdownStartMinutes = minutes
        countdownSeconds = minutes * 60
        countdownTargetTime = android.os.SystemClock.elapsedRealtime() + (minutes * 60 * 1000L)
        lastAnnouncedSecond = countdownSeconds  // Prevent duplicate announcement at same second
        countdownRunning = true
        speak("$minutes minute${if (minutes > 1) "s" else ""}")

        countdownHandler.postDelayed(countdownRunnable, 50L)
        statusListener?.onCountdownTick(countdownSeconds)
        Log.d(TAG, "Race countdown started: $minutes minutes")
    }

    /**
     * Reset/cancel the race countdown timer.
     */
    fun resetCountdown() {
        countdownHandler.removeCallbacks(countdownRunnable)
        countdownRunning = false
        countdownSeconds = -1  // Back to waiting state
        lastAnnouncedSecond = -1
        speak("reset")
        statusListener?.onCountdownReset()
        Log.d(TAG, "Race countdown reset to waiting state")
    }

    /**
     * Check if countdown is currently running.
     */
    fun isCountdownRunning(): Boolean = countdownRunning
}
