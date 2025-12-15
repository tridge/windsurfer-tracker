package nz.co.tracker.windsurfer.presentation

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.location.Location
import android.os.BatteryManager
import android.os.Bundle
import android.os.IBinder
import android.os.VibrationEffect
import android.os.Vibrator
import android.telephony.TelephonyManager
import android.util.Log
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
    private val batteryPercent = mutableIntStateOf(100)
    private val signalLevel = mutableIntStateOf(-1)
    private val ackRate = mutableFloatStateOf(0f)
    private val settings = mutableStateOf(TrackerSettings())

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            val binder = service as TrackerService.LocalBinder
            trackerService = binder.getService()
            serviceBound = true

            trackerService?.statusListener = object : TrackerService.StatusListener {
                override fun onLocationUpdate(location: Location) {
                    speedKnots.floatValue = (location.speed * 1.94384f).toFloat()  // m/s to knots
                    updateBatteryAndSignal()
                }

                override fun onAckReceived(seq: Int) {
                    // Ack received
                }

                override fun onPacketSent(seq: Int) {
                    // Packet sent
                }

                override fun onConnectionStatus(rate: Float) {
                    ackRate.floatValue = rate
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
                startTracking()
            }
        } else {
            Toast.makeText(this, "Location permission required", Toast.LENGTH_LONG).show()
        }
    }

    private val backgroundLocationRequest = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            startTracking()
        } else {
            Toast.makeText(this, "Background location required for tracking", Toast.LENGTH_LONG).show()
        }
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
            }
        }

        setContent {
            TrackerApp(
                isTracking = isTracking.value,
                isAssistActive = isAssistActive.value,
                speedKnots = speedKnots.floatValue,
                batteryPercent = batteryPercent.intValue,
                signalLevel = signalLevel.intValue,
                ackRate = ackRate.floatValue,
                settings = settings.value,
                onToggleTracking = { toggleTracking() },
                onAssistToggle = { toggleAssist() },
                onSaveSettings = { newSettings ->
                    lifecycleScope.launch {
                        settingsRepository.updateSettings(newSettings)
                    }
                }
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

    private fun bindToServiceIfRunning() {
        val intent = Intent(this, TrackerService::class.java)
        bindService(intent, serviceConnection, 0)
    }

    private fun toggleTracking() {
        if (isTracking.value) {
            stopTracking()
        } else {
            checkPermissionsAndStart()
        }
    }

    private fun checkPermissionsAndStart() {
        when {
            hasLocationPermissions() -> startTracking()
            else -> requestLocationPermissions()
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
                Manifest.permission.ACCESS_COARSE_LOCATION
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
            putExtra("high_frequency_mode", currentSettings.highFrequencyMode)
        }

        startForegroundService(intent)
        bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)

        isTracking.value = true
        Log.d(TAG, "Starting tracking with ID=${currentSettings.sailorId}")
    }

    private fun stopTracking() {
        trackerService?.stopService()

        if (serviceBound) {
            unbindService(serviceConnection)
            serviceBound = false
        }

        isTracking.value = false
        isAssistActive.value = false
        speedKnots.floatValue = 0f
        ackRate.floatValue = 0f
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
}
