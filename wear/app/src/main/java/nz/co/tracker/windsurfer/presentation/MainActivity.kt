package nz.co.tracker.windsurfer.presentation

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.location.Location
import android.net.Uri
import android.os.BatteryManager
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.provider.Settings
import android.telephony.TelephonyManager
import android.util.Log
import android.view.KeyEvent
import android.view.WindowManager
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.*
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import nz.co.tracker.windsurfer.SettingsRepository
import nz.co.tracker.windsurfer.TrackerService
import nz.co.tracker.windsurfer.TrackerSettings

class MainActivity : ComponentActivity() {

    companion object {
        private const val TAG = "MainActivity"
    }

    private var trackerService: TrackerService? = null
    private var serviceBound = false

    private lateinit var settingsRepository: SettingsRepository

    // UI state
    private val isTracking = mutableStateOf(false)
    private val isAssistActive = mutableStateOf(false)
    private val speedKnots = mutableFloatStateOf(0f)
    private val distanceMeters = mutableFloatStateOf(0f)
    private val batteryPercent = mutableIntStateOf(100)
    private val signalLevel = mutableIntStateOf(-1)
    private val ackRate = mutableFloatStateOf(0f)
    private val lastAckTime = mutableLongStateOf(0L)  // Time of last ACK in millis
    private val eventName = mutableStateOf("")
    private val errorMessage = mutableStateOf<String?>(null)
    private val assistEnabled = mutableStateOf(true)  // Whether assist button should be shown
    private val settings = mutableStateOf(TrackerSettings())

    // Race countdown timer state
    private val countdownSeconds = mutableStateOf<Int?>(null)
    private var lastCrownPressTime = 0L

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            val binder = service as TrackerService.LocalBinder
            trackerService = binder.getService()
            serviceBound = true

            trackerService?.statusListener = object : TrackerService.StatusListener {
                override fun onLocationUpdate(location: Location, totalDistanceMeters: Float) {
                    speedKnots.floatValue = (location.speed * 1.94384f).toFloat()  // m/s to knots
                    distanceMeters.floatValue = totalDistanceMeters
                    updateBatteryAndSignal()
                }

                override fun onAckReceived(seq: Int) {
                    // Clear error on successful ACK
                    errorMessage.value = null
                    lastAckTime.longValue = System.currentTimeMillis()
                }

                override fun onPacketSent(seq: Int) {
                    // Packet sent
                }

                override fun onConnectionStatus(rate: Float) {
                    ackRate.floatValue = rate
                }

                override fun onEventName(name: String) {
                    eventName.value = name
                }

                override fun onError(message: String) {
                    errorMessage.value = message
                }

                override fun onStatusLine(status: String) {
                    eventName.value = status
                }

                override fun onAssistEnabled(enabled: Boolean) {
                    assistEnabled.value = enabled
                }

                override fun onCountdownTick(secondsRemaining: Int) {
                    countdownSeconds.value = secondsRemaining
                }

                override fun onCountdownFinished() {
                    countdownSeconds.value = 0
                    // Keep showing 0:00 briefly, then clear and allow screen to sleep
                    Handler(Looper.getMainLooper()).postDelayed({
                        countdownSeconds.value = null
                        // Turn off screen wake after race starts - no longer need quick timer access
                        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                    }, 3000)
                }

            }

            isTracking.value = trackerService?.isTracking() == true

            // Sync assist state - push local state to service (for case when assist activated before service started)
            if (isAssistActive.value) {
                trackerService?.requestAssist(true)
            } else {
                isAssistActive.value = trackerService?.isAssistActive() == true
            }
            Log.d(TAG, "Service connected, tracking=${isTracking.value}, assist=${isAssistActive.value}")
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            trackerService = null
            serviceBound = false
            isTracking.value = false
            Log.d(TAG, "Service disconnected")
        }
    }

    private val locationPermissionRequest = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        val fineLocationGranted = permissions[Manifest.permission.ACCESS_FINE_LOCATION] == true
        val backgroundLocationGranted = permissions[Manifest.permission.ACCESS_BACKGROUND_LOCATION] == true

        if (fineLocationGranted) {
            if (!backgroundLocationGranted) {
                // Request background location separately (Android 10+ requirement)
                requestBackgroundLocation()
            } else {
                checkBatteryOptimizationAndStart()
            }
        } else {
            Toast.makeText(this, "Location permission required", Toast.LENGTH_LONG).show()
        }
    }

    private val backgroundLocationRequest = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            checkBatteryOptimizationAndStart()
        } else {
            Toast.makeText(this, "Background location required for tracking", Toast.LENGTH_LONG).show()
        }
    }

    private val batteryOptimizationRequest = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) {
        // Start tracking regardless of result - user made their choice
        startTracking()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        settingsRepository = SettingsRepository(this)

        // Load settings
        lifecycleScope.launch {
            settings.value = settingsRepository.settingsFlow.first()
        }

        // Observe settings changes
        lifecycleScope.launch {
            settingsRepository.settingsFlow.collect { newSettings ->
                settings.value = newSettings
                updateScreenWakeState()
            }
        }

        setContent {
            TrackerApp(
                isTracking = isTracking.value,
                isAssistActive = isAssistActive.value,
                assistEnabled = assistEnabled.value,
                speedKnots = speedKnots.floatValue,
                distanceMeters = distanceMeters.floatValue,
                batteryPercent = batteryPercent.intValue,
                signalLevel = signalLevel.intValue,
                ackRate = ackRate.floatValue,
                lastAckTime = lastAckTime.longValue,
                eventName = eventName.value,
                errorMessage = errorMessage.value,
                settings = settings.value,
                countdownSeconds = countdownSeconds.value,
                onToggleTracking = { toggleTracking() },
                onAssistToggle = { toggleAssist() },
                onSaveSettings = { newSettings ->
                    lifecycleScope.launch {
                        settingsRepository.updateSettings(newSettings)
                        // Restart tracking if already running to apply new settings
                        if (isTracking.value) {
                            Log.d(TAG, "Restarting tracking to apply new settings")
                            stopTracking()
                            startTracking()
                        }
                    }
                },
                onTimerStart = { startRaceTimer() },
                onTimerReset = { resetRaceTimer() }
            )
        }

        // Check if service is already running
        bindToServiceIfRunning()
        updateBatteryAndSignal()
    }

    override fun onResume() {
        super.onResume()
        updateBatteryAndSignal()
    }

    override fun onDestroy() {
        super.onDestroy()
        if (serviceBound) {
            unbindService(serviceConnection)
            serviceBound = false
        }
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_STEM_PRIMARY) {
            // Only handle if race timer is enabled in settings and tracking is active
            if (!settings.value.raceTimerEnabled || !isTracking.value) {
                return super.onKeyDown(keyCode, event)
            }

            val now = System.currentTimeMillis()
            val timeSinceLastPress = now - lastCrownPressTime
            lastCrownPressTime = now

            if (timeSinceLastPress < 500) {
                // Double-press: reset countdown
                trackerService?.resetCountdown()
                countdownSeconds.value = null
                Log.d(TAG, "Crown double-press: reset countdown")
            } else {
                // Single press: start countdown (if not already running)
                if (trackerService?.isCountdownRunning() != true) {
                    trackerService?.startCountdown(settings.value.raceTimerMinutes)
                    Log.d(TAG, "Crown single-press: start countdown")
                }
            }
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    private fun bindToServiceIfRunning() {
        val intent = Intent(this, TrackerService::class.java)
        bindService(intent, serviceConnection, 0)
    }

    private fun toggleTracking() {
        errorMessage.value = null  // Clear any previous error
        if (isTracking.value) {
            stopTracking()
        } else {
            checkPermissionsAndStart()
        }
    }

    private fun checkPermissionsAndStart() {
        when {
            hasLocationPermissions() -> checkBatteryOptimizationAndStart()
            else -> requestLocationPermissions()
        }
    }

    private fun checkBatteryOptimizationAndStart() {
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (!powerManager.isIgnoringBatteryOptimizations(packageName)) {
            // Request battery optimization exemption
            try {
                val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                    data = Uri.parse("package:$packageName")
                }
                batteryOptimizationRequest.launch(intent)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to open battery optimization settings", e)
                Toast.makeText(this, "Please disable battery optimization in Settings", Toast.LENGTH_LONG).show()
                startTracking()
            }
        } else {
            startTracking()
        }
    }

    private fun hasLocationPermissions(): Boolean {
        val fineLocation = ContextCompat.checkSelfPermission(
            this, Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED

        val backgroundLocation = ContextCompat.checkSelfPermission(
            this, Manifest.permission.ACCESS_BACKGROUND_LOCATION
        ) == PackageManager.PERMISSION_GRANTED

        return fineLocation && backgroundLocation
    }

    private fun requestLocationPermissions() {
        locationPermissionRequest.launch(
            arrayOf(
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION,
                Manifest.permission.BODY_SENSORS  // Optional - for heart rate
            )
        )
    }

    private fun requestBackgroundLocation() {
        backgroundLocationRequest.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
    }

    private fun startTracking() {
        val currentSettings = settings.value

        val intent = Intent(this, TrackerService::class.java).apply {
            putExtra("server_host", currentSettings.serverHost)
            putExtra("server_port", TrackerService.DEFAULT_SERVER_PORT)
            putExtra("sailor_id", currentSettings.sailorId)
            putExtra("role", currentSettings.role)
            putExtra("password", currentSettings.password)
            putExtra("event_id", currentSettings.eventId)
            putExtra("high_frequency_mode", currentSettings.highFrequencyMode)
            putExtra("heart_rate_enabled", currentSettings.heartRateEnabled)
            putExtra("tracker_beep", currentSettings.trackerBeep)
            putExtra("race_timer_enabled", currentSettings.raceTimerEnabled)
            putExtra("race_timer_minutes", currentSettings.raceTimerMinutes)
            putExtra("race_timer_tap_g_force", currentSettings.raceTimerTapGForce)
        }

        startForegroundService(intent)
        bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)

        isTracking.value = true
        updateScreenWakeState()
        Log.d(TAG, "Starting tracking with ID=${currentSettings.sailorId}")
    }

    private fun stopTracking() {
        // Send stop notification to server, then clean up
        trackerService?.requestGracefulStop {
            finishStopTracking()
        } ?: finishStopTracking()
    }

    private fun finishStopTracking() {
        trackerService?.stopService()

        if (serviceBound) {
            unbindService(serviceConnection)
            serviceBound = false
        }

        isTracking.value = false
        isAssistActive.value = false
        speedKnots.floatValue = 0f
        distanceMeters.floatValue = 0f
        ackRate.floatValue = 0f
        updateScreenWakeState()
        Log.d(TAG, "Stopped tracking")
    }

    private fun toggleAssist() {
        val newState = !isAssistActive.value
        isAssistActive.value = newState

        // If activating assist and not already tracking, start tracking first
        if (newState && !isTracking.value) {
            checkPermissionsAndStart()
        }

        trackerService?.requestAssist(newState)

        // Haptic feedback
        val vibrator = getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
        val duration = if (newState) 300L else 100L  // Long vibration to activate, short to cancel
        vibrator.vibrate(VibrationEffect.createOneShot(duration, VibrationEffect.DEFAULT_AMPLITUDE))

        Log.d(TAG, "Assist ${if (newState) "ACTIVATED" else "cancelled"}")
    }

    private fun startRaceTimer() {
        if (trackerService?.isCountdownRunning() != true) {
            trackerService?.startCountdown(settings.value.raceTimerMinutes)
            Log.d(TAG, "Race timer started: ${settings.value.raceTimerMinutes} minutes")
        }
    }

    private fun resetRaceTimer() {
        trackerService?.resetCountdown()
        countdownSeconds.value = null
        Log.d(TAG, "Race timer reset")
    }

    private fun updateBatteryAndSignal() {
        // Battery
        try {
            val batteryManager = getSystemService(Context.BATTERY_SERVICE) as BatteryManager
            batteryPercent.intValue = batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to get battery", e)
        }

        // Signal
        try {
            val telephonyManager = getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
            signalLevel.intValue = telephonyManager.signalStrength?.level ?: -1
        } catch (e: Exception) {
            signalLevel.intValue = -1
        }
    }

    /**
     * Keep screen on when race timer is enabled and tracking is active.
     * This allows quick access to start the countdown when the horn sounds.
     */
    private fun updateScreenWakeState() {
        val shouldKeepScreenOn = isTracking.value && settings.value.raceTimerEnabled
        if (shouldKeepScreenOn) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            Log.d(TAG, "Screen wake lock enabled (race timer mode)")
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            Log.d(TAG, "Screen wake lock disabled")
        }
    }
}
