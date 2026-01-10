package nz.co.tracker.windsurfer

import android.Manifest
import android.content.*
import android.content.pm.PackageManager
import android.location.Location
import android.net.Uri
import android.os.Bundle
import android.os.IBinder
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import com.google.android.material.snackbar.Snackbar
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch
import nz.co.tracker.windsurfer.databinding.ActivityMainBinding
import java.text.SimpleDateFormat
import java.util.*

class MainActivity : AppCompatActivity(), TrackerService.StatusListener {
    
    companion object {
        private const val TAG = "MainActivity"
        private const val PREFS_NAME = "tracker_prefs"
    }
    
    private lateinit var binding: ActivityMainBinding
    private var trackerService: TrackerService? = null
    private var serviceBound = false
    private var bindingInProgress = false
    private lateinit var updateChecker: UpdateChecker
    private var currentEventName: String = ""
    
    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            val binder = service as TrackerService.LocalBinder
            trackerService = binder.getService()
            trackerService?.statusListener = this@MainActivity
            serviceBound = true
            bindingInProgress = false
            
            // Check if this is a stale service (bound but not tracking)
            if (trackerService?.isTracking() != true) {
                Log.d(TAG, "Found stale service, cleaning up")
                stopService(Intent(this@MainActivity, TrackerService::class.java))
            }
            
            updateUI()
            Log.d(TAG, "Service connected, tracking=${trackerService?.isTracking()}")
        }
        
        override fun onServiceDisconnected(name: ComponentName?) {
            trackerService?.statusListener = null
            trackerService = null
            serviceBound = false
            bindingInProgress = false
            Log.d(TAG, "Service disconnected")
        }
    }
    
    private val locationPermissionRequest = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        when {
            permissions[Manifest.permission.ACCESS_FINE_LOCATION] == true -> {
                checkBackgroundLocationPermission()
            }
            else -> {
                Toast.makeText(this, "Location permission required for tracking", Toast.LENGTH_LONG).show()
            }
        }
    }
    
    private val backgroundLocationRequest = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            checkNotificationPermission()
        } else {
            // Can still work, just warn user
            Toast.makeText(this, "Background location recommended for reliable tracking", Toast.LENGTH_LONG).show()
            checkNotificationPermission()
        }
    }
    
    private val notificationPermissionRequest = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (!granted) {
            Toast.makeText(this, "Notifications help you know tracking is active", Toast.LENGTH_SHORT).show()
        }
        // Check battery optimization next
        checkBatteryOptimization()
    }

    private val batteryOptimizationRequest = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) {
        // Check if user actually disabled it
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (!powerManager.isIgnoringBatteryOptimizations(packageName)) {
            Toast.makeText(this, "Battery optimization still enabled - tracking may be unreliable", Toast.LENGTH_LONG).show()
        }
        startTrackerService()
    }
    
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        updateChecker = UpdateChecker(this)

        setupUI()
        loadPreferences()

        // Check for updates on startup (sideload builds only)
        if (BuildConfig.ENABLE_SELF_UPDATE) {
            checkForUpdatesOnStartup()
        }

        // Check if we should auto-resume tracking
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)

        // Auto-open settings if ID or password is missing
        val sailorId = prefs.getString("sailor_id", "") ?: ""
        val password = prefs.getString("password", "") ?: ""
        if (sailorId.isEmpty() || password.isEmpty()) {
            // Delay slightly to ensure UI is ready
            binding.root.post {
                showSettingsDialog()
            }
        } else if (prefs.getBoolean("tracking_active", false)) {
            // Was tracking before - restart (but verify permissions first)
            Log.d(TAG, "Auto-resuming tracking from saved state")
            // Must check location permission before starting foreground service on Android 14+
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED) {
                checkBatteryOptimization()
            } else {
                // Permission was revoked - clear tracking state
                Log.d(TAG, "Location permission revoked, cannot auto-resume")
                prefs.edit().putBoolean("tracking_active", false).apply()
            }
        }
    }

    private fun checkForUpdatesOnStartup() {
        lifecycleScope.launch {
            when (val result = updateChecker.checkForUpdate()) {
                is UpdateCheckResult.UpdateAvailable -> {
                    if (!updateChecker.isVersionSkipped(result.versionInfo.versionCode)) {
                        showUpdateDialog(result.versionInfo, allowSkip = true)
                    }
                }
                is UpdateCheckResult.NoUpdate -> { /* Silent on startup */ }
                is UpdateCheckResult.Error -> { /* Silent on startup */ }
            }
        }
    }

    private fun checkForUpdatesManual() {
        lifecycleScope.launch {
            when (val result = updateChecker.checkForUpdate()) {
                is UpdateCheckResult.UpdateAvailable -> {
                    updateChecker.clearSkippedVersion()
                    showUpdateDialog(result.versionInfo, allowSkip = false)
                }
                is UpdateCheckResult.NoUpdate -> {
                    Toast.makeText(this@MainActivity, "You have the latest version", Toast.LENGTH_SHORT).show()
                }
                is UpdateCheckResult.Error -> {
                    Toast.makeText(this@MainActivity, "Update check failed: ${result.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    private fun showUpdateDialog(update: VersionInfo, allowSkip: Boolean) {
        val message = buildString {
            append("Version ${update.version} is available.\n")
            append("You have version ${updateChecker.getCurrentVersionString()}.\n")
            if (update.changelog.isNotBlank()) {
                append("\nChanges:\n${update.changelog}")
            }
        }

        val builder = AlertDialog.Builder(this, android.R.style.Theme_Material_Light_Dialog_Alert)
            .setTitle("Update Available")
            .setMessage(message)
            .setPositiveButton("UPDATE NOW") { _, _ ->
                downloadUpdate(update)
            }
            .setNegativeButton("LATER", null)

        if (allowSkip) {
            builder.setNeutralButton("SKIP VERSION") { _, _ ->
                updateChecker.skipVersion(update.versionCode)
                Toast.makeText(this, "You can check for updates in Settings", Toast.LENGTH_SHORT).show()
            }
        }

        builder.show()
    }

    private fun downloadUpdate(update: VersionInfo) {
        Toast.makeText(this, "Downloading update...", Toast.LENGTH_SHORT).show()

        updateChecker.downloadAndInstall(
            update,
            onProgress = { /* Could show progress */ },
            onComplete = {
                Toast.makeText(this, "Download complete, installing...", Toast.LENGTH_SHORT).show()
            },
            onError = { error ->
                runOnUiThread {
                    Toast.makeText(this, "Update failed: $error", Toast.LENGTH_LONG).show()
                }
            }
        )
    }
    
    override fun onStart() {
        super.onStart()
        // Only bind here if we didn't just start the service in onCreate
        if (!serviceBound && !bindingInProgress) {
            // Try to bind to existing service (don't auto-create)
            Intent(this, TrackerService::class.java).also { intent ->
                bindService(intent, serviceConnection, 0)
            }
        }
    }
    
    override fun onStop() {
        super.onStop()
        if (serviceBound) {
            trackerService?.statusListener = null
            unbindService(serviceConnection)
            serviceBound = false
        }
    }
    
    private fun setupUI() {
        // Start/Stop button
        binding.btnStartStop.setOnClickListener {
            if (trackerService?.isTracking() == true) {
                // Show high-contrast confirmation dialog for outdoor use
                val dialog = AlertDialog.Builder(this, android.R.style.Theme_Material_Light_Dialog_Alert)
                    .setTitle("Stop Tracking?")
                    .setMessage("Are you sure you want to stop tracking? Your position will no longer be reported.")
                    .setPositiveButton("STOP") { _, _ ->
                        stopTrackerService()
                    }
                    .setNegativeButton("CANCEL", null)
                    .create()
                
                dialog.setOnShowListener {
                    // High contrast button colors for outdoor readability
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE)?.apply {
                        setTextColor(0xFFFFFFFF.toInt())
                        setBackgroundColor(0xFFCC0000.toInt())  // Dark red
                        textSize = 18f
                    }
                    dialog.getButton(AlertDialog.BUTTON_NEGATIVE)?.apply {
                        setTextColor(0xFF000000.toInt())
                        setBackgroundColor(0xFFCCCCCC.toInt())  // Light gray
                        textSize = 18f
                    }
                }
                dialog.show()
            } else {
                checkPermissionsAndStart()
            }
        }
        
        // Assist button - long press to activate/deactivate
        binding.btnAssist.setOnLongClickListener {
            val service = trackerService
            if (service != null && service.isTracking()) {
                val newState = !service.isAssistActive()
                service.requestAssist(newState)
                updateAssistButton(newState)
                
                // Vibrate to confirm
                @Suppress("DEPRECATION")
                val vibrator = getSystemService(VIBRATOR_SERVICE) as android.os.Vibrator
                if (newState) {
                    // Long vibration for activation
                    vibrator.vibrate(longArrayOf(0, 300, 100, 300), -1)
                    Toast.makeText(this, "ASSIST REQUEST ACTIVATED", Toast.LENGTH_LONG).show()
                } else {
                    // Short vibration for deactivation
                    vibrator.vibrate(100)
                    Toast.makeText(this, "Assist request cancelled", Toast.LENGTH_SHORT).show()
                }
            } else {
                Toast.makeText(this, "Start tracking first", Toast.LENGTH_SHORT).show()
            }
            true  // Consume the long press
        }
        
        // Regular tap on assist button shows hint
        binding.btnAssist.setOnClickListener {
            if (trackerService?.isTracking() != true) {
                Toast.makeText(this, "Start tracking first", Toast.LENGTH_SHORT).show()
            } else if (trackerService?.isAssistActive() == true) {
                Toast.makeText(this, "Long press to CANCEL assist request", Toast.LENGTH_SHORT).show()
            } else {
                Toast.makeText(this, "Long press to request assistance", Toast.LENGTH_SHORT).show()
            }
        }
        
        // Settings button
        binding.btnSettings.setOnClickListener {
            showSettingsDialog()
        }

        // Save preferences when main screen fields change (so settings dialog stays in sync)
        // Idle screen is now read-only - no text change listeners needed
    }

    private fun getDefaultSailorId(): String {
        // Return empty to require user to set an ID
        return ""
    }

    // Flag to prevent TextWatcher from saving during loadPreferences()
    private var isLoadingPreferences = false

    private fun loadPreferences() {
        isLoadingPreferences = true
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)

        // Display sailor name on idle screen (read-only)
        val sailorId = prefs.getString("sailor_id", getDefaultSailorId()) ?: getDefaultSailorId()
        binding.tvIdleSailorName.text = if (sailorId.isNotEmpty()) sailorId else "(not set)"

        // Migrate old server address for early beta testers
        var serverHost = prefs.getString("server_host", TrackerService.DEFAULT_SERVER_HOST) ?: TrackerService.DEFAULT_SERVER_HOST
        if (serverHost == "track.tridgell.net") {
            serverHost = "wstracker.org"
            prefs.edit().putString("server_host", serverHost).apply()
        }

        isLoadingPreferences = false
        updateIdleScreen()
    }

    private fun updateIdleScreen() {
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
        val eventId = prefs.getInt("event_id", 2)
        val serverHost = prefs.getString("server_host", TrackerService.DEFAULT_SERVER_HOST) ?: TrackerService.DEFAULT_SERVER_HOST
        val serverPort = prefs.getInt("server_port", TrackerService.DEFAULT_SERVER_PORT)

        // Load saved event name from preferences if we don't have it in memory
        if (currentEventName.isEmpty()) {
            currentEventName = prefs.getString("event_name", "") ?: ""
        }

        // Event display (name + ID) - show initially, may update async
        val eventText = if (currentEventName.isNotEmpty()) {
            "$currentEventName (ID: $eventId)"
        } else {
            "Event $eventId"
        }
        binding.tvIdleEventName.text = eventText

        // Async fetch event name if we don't have it cached
        if (currentEventName.isEmpty()) {
            lifecycleScope.launch {
                val fetcher = EventFetcher()
                val events = fetcher.fetchEvents(serverHost, serverPort)
                val event = events.firstOrNull { it.eid == eventId }
                if (event != null) {
                    currentEventName = event.name
                    // Save to preferences
                    prefs.edit().putString("event_name", event.name).apply()
                    // Update display
                    binding.tvIdleEventName.text = "$currentEventName (ID: $eventId)"
                }
            }
        }

        // Live tracking link (just URL, label is separate)
        val linkText = "https://$serverHost/event.html?eid=$eventId"
        binding.tvLiveTrackingLink.text = linkText
    }
    
    // savePreferences removed - idle screen is now read-only, all edits in settings dialog

    private fun checkPermissionsAndStart() {
        when {
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) 
                    == PackageManager.PERMISSION_GRANTED -> {
                checkBackgroundLocationPermission()
            }
            shouldShowRequestPermissionRationale(Manifest.permission.ACCESS_FINE_LOCATION) -> {
                AlertDialog.Builder(this)
                    .setTitle("Location Permission Required")
                    .setMessage("This app needs location permission to track your position during the race.")
                    .setPositiveButton("Grant") { _, _ ->
                        requestLocationPermission()
                    }
                    .setNegativeButton("Cancel", null)
                    .show()
            }
            else -> {
                requestLocationPermission()
            }
        }
    }
    
    private fun requestLocationPermission() {
        locationPermissionRequest.launch(arrayOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION
        ))
    }
    
    private fun checkBackgroundLocationPermission() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_BACKGROUND_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            AlertDialog.Builder(this)
                .setTitle("Background Location")
                .setMessage("For reliable tracking even when the app is in the background, please grant 'Allow all the time' location access.")
                .setPositiveButton("Grant") { _, _ ->
                    backgroundLocationRequest.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
                }
                .setNegativeButton("Skip") { _, _ ->
                    checkNotificationPermission()
                }
                .show()
        } else {
            checkNotificationPermission()
        }
    }
    
    private fun checkNotificationPermission() {
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
                notificationPermissionRequest.launch(Manifest.permission.POST_NOTIFICATIONS)
                return
            }
        }
        checkBatteryOptimization()
    }

    private fun checkBatteryOptimization() {
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (!powerManager.isIgnoringBatteryOptimizations(packageName)) {
            AlertDialog.Builder(this, android.R.style.Theme_Material_Light_Dialog_Alert)
                .setTitle("Battery Optimization")
                .setMessage("For reliable GPS tracking, please disable battery optimization for this app.\n\nWithout this, Android may stop location updates when the screen is off.")
                .setPositiveButton("DISABLE") { _, _ ->
                    try {
                        val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                            data = Uri.parse("package:$packageName")
                        }
                        batteryOptimizationRequest.launch(intent)
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to open battery optimization settings", e)
                        Toast.makeText(this, "Please manually disable battery optimization in Settings", Toast.LENGTH_LONG).show()
                        startTrackerService()
                    }
                }
                .setNegativeButton("SKIP") { _, _ ->
                    Toast.makeText(this, "Tracking may be unreliable with battery optimization enabled", Toast.LENGTH_LONG).show()
                    startTrackerService()
                }
                .setCancelable(false)
                .create()
                .apply {
                    setOnShowListener {
                        getButton(AlertDialog.BUTTON_POSITIVE)?.apply {
                            setTextColor(0xFFFFFFFF.toInt())
                            setBackgroundColor(0xFF00AA00.toInt())
                            textSize = 18f
                        }
                        getButton(AlertDialog.BUTTON_NEGATIVE)?.apply {
                            setTextColor(0xFF000000.toInt())
                            setBackgroundColor(0xFFCCCCCC.toInt())
                            textSize = 18f
                        }
                    }
                }
                .show()
        } else {
            startTrackerService()
        }
    }
    
    private fun startTrackerService() {
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)

        // Validate ID and password before starting (read from preferences)
        val sailorId = (prefs.getString("sailor_id", "") ?: "").trim()
        val password = prefs.getString("password", "") ?: ""

        if (sailorId.isEmpty() && password.isEmpty()) {
            Toast.makeText(this, "Name and password are required. Please configure in Settings.", Toast.LENGTH_LONG).show()
            return
        }
        if (sailorId.isEmpty()) {
            Toast.makeText(this, "Your name is required. Please configure in Settings.", Toast.LENGTH_LONG).show()
            return
        }
        if (password.isEmpty()) {
            Toast.makeText(this, "Password is required. Please configure in Settings.", Toast.LENGTH_LONG).show()
            return
        }

        // Save tracking state
        prefs.edit().putBoolean("tracking_active", true).apply()

        // Get server settings from preferences (no longer on main screen)
        val serverHost = prefs.getString("server_host", TrackerService.DEFAULT_SERVER_HOST) ?: TrackerService.DEFAULT_SERVER_HOST
        val serverPort = prefs.getInt("server_port", TrackerService.DEFAULT_SERVER_PORT)

        val intent = Intent(this, TrackerService::class.java).apply {
            putExtra("sailor_id", sailorId)
            putExtra("server_host", serverHost)
            putExtra("server_port", serverPort)
            putExtra("role", prefs.getString("role", "sailor"))
            putExtra("password", prefs.getString("password", ""))
            putExtra("high_frequency_mode", prefs.getBoolean("high_frequency_mode", false))
        }

        ContextCompat.startForegroundService(this, intent)
        bindingInProgress = true
        bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)

        binding.btnStartStop.text = "Stop Tracking"
        binding.statusGroup.visibility = View.VISIBLE
        binding.configGroup.visibility = View.GONE
    }
    
    private fun stopTrackerService() {
        // Send stop notification to server, then clean up
        trackerService?.requestGracefulStop {
            finishStopTrackerService()
        } ?: finishStopTrackerService()
    }

    private fun finishStopTrackerService() {
        // Clear tracking state
        getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit()
            .putBoolean("tracking_active", false)
            .apply()

        trackerService?.statusListener = null
        if (serviceBound) {
            unbindService(serviceConnection)
            serviceBound = false
        }
        bindingInProgress = false
        stopService(Intent(this, TrackerService::class.java))
        trackerService = null

        binding.btnStartStop.text = "Start Tracking"
        binding.statusGroup.visibility = View.GONE
        binding.configGroup.visibility = View.VISIBLE
        updateAssistButton(false)
    }
    
    private fun updateUI() {
        val service = trackerService
        if (service != null && service.isTracking()) {
            // Service is bound AND actively tracking
            binding.btnStartStop.text = "Stop Tracking"
            binding.statusGroup.visibility = View.VISIBLE
            binding.configGroup.visibility = View.GONE
            updateAssistButton(service.isAssistActive())

            // Show frequency mode indicator
            val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            val highFrequencyMode = prefs.getBoolean("high_frequency_mode", false)
            binding.tv1HzIndicator.text = if (highFrequencyMode) "1Hz MODE" else "0.1Hz MODE"
            binding.tv1HzIndicator.visibility = View.VISIBLE

            service.getLastLocation()?.let { loc ->
                updateLocationDisplay(loc)
            }

            updateConnectionStatus(service.getAckRate())
        } else {
            // Service is not tracking - show config screen
            binding.btnStartStop.text = "Start Tracking"
            binding.statusGroup.visibility = View.GONE
            binding.configGroup.visibility = View.VISIBLE
            updateAssistButton(false)
        }
    }
    
    private fun updateAssistButton(active: Boolean) {
        if (active) {
            binding.btnAssist.text = "⚠ ASSISTANCE REQUESTED ⚠\n\nLong press to cancel"
            binding.btnAssist.setBackgroundColor(0xFFFF0000.toInt())  // Bright red
            binding.btnAssist.setTextColor(0xFFFFFFFF.toInt())        // White text
            
            // Keep screen on while assistance is requested
            window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            
            // Start pulsing animation
            binding.btnAssist.animate()
                .alpha(0.7f)
                .setDuration(500)
                .withEndAction(object : Runnable {
                    override fun run() {
                        if (trackerService?.isAssistActive() == true) {
                            binding.btnAssist.animate()
                                .alpha(1.0f)
                                .setDuration(500)
                                .withEndAction(this)
                                .start()
                        }
                    }
                })
                .start()
        } else {
            binding.btnAssist.text = "REQUEST ASSISTANCE\n\nLong press to activate"
            binding.btnAssist.setBackgroundColor(0xFF00AA00.toInt())  // Bright green
            binding.btnAssist.setTextColor(0xFF000000.toInt())        // Black text
            binding.btnAssist.alpha = 1.0f
            binding.btnAssist.animate().cancel()
            
            // Allow screen to turn off again
            window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }
    
    private fun updateLocationDisplay(location: Location, totalDistanceMeters: Float = 0f) {
        val latDir = if (location.latitude < 0) "S" else "N"
        val lonDir = if (location.longitude < 0) "W" else "E"

        binding.tvPosition.text = String.format(
            "%.5f°%s %.5f°%s",
            Math.abs(location.latitude), latDir,
            Math.abs(location.longitude), lonDir
        )

        val speedKnots = location.speed * 1.94384
        binding.tvSpeed.text = String.format("%.1f kn", speedKnots)
        binding.tvHeading.text = String.format("%03d°", location.bearing.toInt())
        binding.tvDistance.text = String.format("%.1f km", totalDistanceMeters / 1000f)

        val sdf = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
        binding.tvLastUpdate.text = sdf.format(Date())
    }
    
    private fun updateConnectionStatus(ackRate: Float) {
        val percentage = (ackRate * 100).toInt().coerceIn(0, 100)
        binding.tvAckRate.text = "$percentage%"
        
        // High contrast colors for outdoor readability
        val color = when {
            percentage >= 80 -> 0xFF008800.toInt()  // Dark green
            percentage >= 50 -> 0xFFCC6600.toInt()  // Dark orange
            else -> 0xFFCC0000.toInt()              // Dark red
        }
        binding.tvAckRate.setTextColor(color)
    }
    
    private fun showSettingsDialog() {
        // Create a simple dialog with EditTexts for settings - high contrast for outdoor use
        val layout = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 32, 48, 16)
            setBackgroundColor(0xFFFFFFFF.toInt())
        }
        
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
        
        val sailorIdLabel = android.widget.TextView(this).apply {
            text = "Your Name"
            setTextColor(0xFF000000.toInt())
            textSize = 16f
        }
        val sailorIdInput = android.widget.EditText(this).apply {
            setText(prefs.getString("sailor_id", getDefaultSailorId()))
            inputType = android.text.InputType.TYPE_CLASS_TEXT
            setTextColor(0xFF000000.toInt())
            setBackgroundColor(0xFFEEEEEE.toInt())
            textSize = 18f
            setPadding(16, 16, 16, 16)
        }
        
        val roleLabel = android.widget.TextView(this).apply {
            text = "Role"
            setPadding(0, 24, 0, 8)
            setTextColor(0xFF000000.toInt())
            textSize = 16f
        }
        
        val roleOptions = arrayOf("Sailor", "Support", "Spectator")
        val roleValues = arrayOf("sailor", "support", "spectator")
        val currentRole = prefs.getString("role", "sailor") ?: "sailor"
        var selectedRoleIndex = roleValues.indexOf(currentRole).coerceAtLeast(0)
        
        val roleSpinner = android.widget.Spinner(this).apply {
            adapter = android.widget.ArrayAdapter(
                this@MainActivity,
                android.R.layout.simple_spinner_dropdown_item,
                roleOptions
            ).also { adapter ->
                adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
            }
            setSelection(selectedRoleIndex)
            setBackgroundColor(0xFFEEEEEE.toInt())
            onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
                override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: android.view.View?, position: Int, id: Long) {
                    selectedRoleIndex = position
                    // Make dropdown text larger and black
                    (view as? android.widget.TextView)?.apply {
                        textSize = 18f
                        setTextColor(0xFF000000.toInt())
                    }
                }
                override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
            }
        }
        
        val serverLabel = android.widget.TextView(this).apply {
            text = "Server Address"
            setPadding(0, 24, 0, 0)
            setTextColor(0xFF000000.toInt())
            textSize = 16f
        }
        val serverInput = android.widget.EditText(this).apply {
            setText(prefs.getString("server_host", TrackerService.DEFAULT_SERVER_HOST))
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI
            setTextColor(0xFF000000.toInt())
            setBackgroundColor(0xFFEEEEEE.toInt())
            textSize = 18f
            setPadding(16, 16, 16, 16)
        }
        
        val portLabel = android.widget.TextView(this).apply {
            text = "Server Port"
            setPadding(0, 24, 0, 0)
            setTextColor(0xFF000000.toInt())
            textSize = 16f
        }
        val portInput = android.widget.EditText(this).apply {
            setText(prefs.getInt("server_port", TrackerService.DEFAULT_SERVER_PORT).toString())
            inputType = android.text.InputType.TYPE_CLASS_NUMBER
            setTextColor(0xFF000000.toInt())
            setBackgroundColor(0xFFEEEEEE.toInt())
            textSize = 18f
            setPadding(16, 16, 16, 16)
        }

        // Event selector
        val eventLabel = android.widget.TextView(this).apply {
            text = "Event"
            setPadding(0, 24, 0, 8)
            setTextColor(0xFF000000.toInt())
            textSize = 16f
        }

        var events: List<EventInfo> = emptyList()
        var selectedEventId = prefs.getInt("event_id", 2)

        val eventSpinner = android.widget.Spinner(this).apply {
            setBackgroundColor(0xFFEEEEEE.toInt())
        }

        val eventLoadingText = android.widget.TextView(this).apply {
            text = "Loading events..."
            setTextColor(0xFF666666.toInt())
            textSize = 14f
            setPadding(0, 8, 0, 0)
        }

        // Function to fetch events from the current server
        fun fetchEvents() {
            val host = serverInput.text.toString().trim()
            val port = portInput.text.toString().toIntOrNull() ?: TrackerService.DEFAULT_SERVER_PORT

            if (host.isEmpty()) {
                eventLoadingText.text = "Enter server address first"
                return
            }

            eventLoadingText.text = "Loading events..."
            eventSpinner.visibility = android.view.View.GONE

            lifecycleScope.launch {
                val fetcher = EventFetcher()
                events = fetcher.fetchEvents(host, port)

                if (events.isNotEmpty()) {
                    val eventNames = events.map { "${it.name} (${it.eid})" }.toTypedArray()
                    val adapter = android.widget.ArrayAdapter(
                        this@MainActivity,
                        android.R.layout.simple_spinner_dropdown_item,
                        eventNames
                    )
                    adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
                    eventSpinner.adapter = adapter

                    // Select current event
                    val currentIndex = events.indexOfFirst { it.eid == selectedEventId }
                    if (currentIndex >= 0) {
                        eventSpinner.setSelection(currentIndex)
                    }

                    eventSpinner.onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
                        override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: android.view.View?, position: Int, id: Long) {
                            selectedEventId = events[position].eid
                            (view as? android.widget.TextView)?.apply {
                                textSize = 18f
                                setTextColor(0xFF000000.toInt())
                            }
                        }
                        override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
                    }

                    eventLoadingText.text = ""
                    eventSpinner.visibility = android.view.View.VISIBLE
                } else {
                    eventLoadingText.text = "Could not load events (using event ID: $selectedEventId)"
                }
            }
        }

        // Refetch events when server address or port changes
        val serverChangeWatcher = object : android.text.TextWatcher {
            private var debounceJob: kotlinx.coroutines.Job? = null
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
            override fun afterTextChanged(s: android.text.Editable?) {
                // Debounce to avoid fetching on every keystroke
                debounceJob?.cancel()
                debounceJob = lifecycleScope.launch {
                    kotlinx.coroutines.delay(500)
                    fetchEvents()
                }
            }
        }
        serverInput.addTextChangedListener(serverChangeWatcher)
        portInput.addTextChangedListener(serverChangeWatcher)

        // Initial fetch
        fetchEvents()

        val passwordLabel = android.widget.TextView(this).apply {
            text = "Password"
            setPadding(0, 24, 0, 0)
            setTextColor(0xFF000000.toInt())
            textSize = 16f
        }
        val passwordInput = android.widget.EditText(this).apply {
            setText(prefs.getString("password", ""))
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
            setTextColor(0xFF000000.toInt())
            setBackgroundColor(0xFFEEEEEE.toInt())
            textSize = 18f
            setPadding(16, 16, 16, 16)
        }
        val showPasswordCheckbox = android.widget.CheckBox(this).apply {
            text = "Show password"
            isChecked = true
            setTextColor(0xFF000000.toInt())
            textSize = 14f
            setOnCheckedChangeListener { _, isChecked ->
                passwordInput.inputType = if (isChecked) {
                    android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
                } else {
                    android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
                }
                // Keep cursor at end
                passwordInput.setSelection(passwordInput.text.length)
            }
        }

        // 1Hz mode checkbox
        val highFrequencyCheckbox = android.widget.CheckBox(this).apply {
            text = "1Hz Mode"
            isChecked = prefs.getBoolean("high_frequency_mode", false)
            setTextColor(0xFF000000.toInt())
            textSize = 14f
            setPadding(0, 24, 0, 0)
        }
        val highFrequencyHint = android.widget.TextView(this).apply {
            text = "Send positions at 1Hz as batched arrays. Higher battery usage."
            setTextColor(0xFF666666.toInt())
            textSize = 12f
            setPadding(48, 0, 0, 16)
        }

        // Tracking buzz checkbox
        val trackerBeepCheckbox = android.widget.CheckBox(this).apply {
            text = "Tracking Buzz"
            isChecked = prefs.getBoolean("tracker_beep", true)
            setTextColor(0xFF000000.toInt())
            textSize = 14f
            setPadding(0, 8, 0, 0)
        }
        val trackerBeepHint = android.widget.TextView(this).apply {
            text = "Vibrate once per minute while tracking to remind you it's running."
            setTextColor(0xFF666666.toInt())
            textSize = 12f
            setPadding(48, 0, 0, 16)
        }

        // Check for Updates button (sideload builds only)
        val updateButton = if (BuildConfig.ENABLE_SELF_UPDATE) {
            android.widget.Button(this).apply {
                text = "Check for Updates"
                setTextColor(0xFF000000.toInt())
                setBackgroundColor(0xFFDDDDDD.toInt())
                textSize = 16f
                setPadding(16, 24, 16, 24)
            }
        } else null

        // Version info label
        val versionLabel = android.widget.TextView(this).apply {
            text = "Version: ${updateChecker.getCurrentVersionString()}"
            setTextColor(0xFF666666.toInt())
            textSize = 12f
            setPadding(0, 8, 0, 16)
        }

        // Order: Name, Password, Event, 1Hz, Role, Server (rarely changed at bottom)
        layout.addView(sailorIdLabel)
        layout.addView(sailorIdInput)
        layout.addView(passwordLabel)
        layout.addView(passwordInput)
        layout.addView(showPasswordCheckbox)
        layout.addView(eventLabel)
        layout.addView(eventSpinner)
        layout.addView(eventLoadingText)
        layout.addView(highFrequencyCheckbox)
        layout.addView(highFrequencyHint)
        layout.addView(trackerBeepCheckbox)
        layout.addView(trackerBeepHint)
        layout.addView(roleLabel)
        layout.addView(roleSpinner)
        layout.addView(serverLabel)
        layout.addView(serverInput)
        layout.addView(portLabel)
        layout.addView(portInput)
        updateButton?.let { layout.addView(it) }
        layout.addView(versionLabel)

        var dialogRef: AlertDialog? = null

        updateButton?.setOnClickListener {
            dialogRef?.dismiss()
            checkForUpdatesManual()
        }

        // Save old values to detect changes
        val oldSailorId = prefs.getString("sailor_id", "") ?: ""
        val oldRole = prefs.getString("role", "sailor") ?: "sailor"
        val oldServerHost = prefs.getString("server_host", TrackerService.DEFAULT_SERVER_HOST) ?: TrackerService.DEFAULT_SERVER_HOST
        val oldServerPort = prefs.getInt("server_port", TrackerService.DEFAULT_SERVER_PORT)
        val oldEventId = prefs.getInt("event_id", 2)
        val oldPassword = prefs.getString("password", "") ?: ""
        val oldHighFrequencyMode = prefs.getBoolean("high_frequency_mode", false)

        // Wrap layout in ScrollView to make it scrollable
        val scrollView = android.widget.ScrollView(this).apply {
            addView(layout)
        }

        val dialog = AlertDialog.Builder(this, android.R.style.Theme_Material_Light_Dialog_Alert)
            .setTitle("Settings")
            .setView(scrollView)
            .setPositiveButton("SAVE", null)  // Set listener later to prevent auto-dismiss
            .setNegativeButton("CANCEL", null)
            .create()

        dialog.setOnShowListener {
            // High contrast button colors
            dialog.getButton(AlertDialog.BUTTON_POSITIVE)?.apply {
                setTextColor(0xFFFFFFFF.toInt())
                setBackgroundColor(0xFF00AA00.toInt())  // Green
                textSize = 18f
                setOnClickListener saveButton@{
                    // Validate inputs
                    val sailorId = sailorIdInput.text.toString().trim()
                    val password = passwordInput.text.toString()
                    val serverHost = serverInput.text.toString().trim()
                    val serverPort = portInput.text.toString().toIntOrNull() ?: TrackerService.DEFAULT_SERVER_PORT

                    if (sailorId.isEmpty() && password.isEmpty()) {
                        Toast.makeText(this@MainActivity, "Name and password are required", Toast.LENGTH_LONG).show()
                        return@saveButton
                    }
                    if (sailorId.isEmpty()) {
                        Toast.makeText(this@MainActivity, "Your name is required", Toast.LENGTH_LONG).show()
                        return@saveButton
                    }
                    if (password.isEmpty()) {
                        Toast.makeText(this@MainActivity, "Password is required", Toast.LENGTH_LONG).show()
                        return@saveButton
                    }

                    // Only check password with server if auth-related fields changed
                    val authFieldsChanged = sailorId != oldSailorId || password != oldPassword || selectedEventId != oldEventId

                    // Function to save settings (called directly or after password check)
                    fun saveSettings() {
                        // Validation passed, save settings
                        val newHighFrequencyMode = highFrequencyCheckbox.isChecked
                        val newTrackerBeep = trackerBeepCheckbox.isChecked
                        val newRole = roleValues[selectedRoleIndex]
                        val newServerHost = serverInput.text.toString()
                        val newServerPort = portInput.text.toString().toIntOrNull() ?: TrackerService.DEFAULT_SERVER_PORT

                        prefs.edit().apply {
                            putString("sailor_id", sailorId)
                            putString("role", newRole)
                            putString("server_host", newServerHost)
                            putInt("server_port", newServerPort)
                            putInt("event_id", selectedEventId)
                            putString("password", password)
                            putBoolean("high_frequency_mode", newHighFrequencyMode)
                            putBoolean("tracker_beep", newTrackerBeep)
                            commit()  // Use commit() not apply() to ensure write completes before loadPreferences()
                        }
                        // Update the idle screen display to keep it in sync
                        binding.tvIdleSailorName.text = if (sailorId.isNotEmpty()) sailorId else "(not set)"
                        updateIdleScreen()  // Update event name and live tracking link

                        // Auto-restart tracking if any settings changed while tracking
                        val isTracking = trackerService?.isTracking() == true
                        val settingsChanged = sailorId != oldSailorId ||
                            newRole != oldRole ||
                            newServerHost != oldServerHost ||
                            newServerPort != oldServerPort ||
                            selectedEventId != oldEventId ||
                            password != oldPassword ||
                            newHighFrequencyMode != oldHighFrequencyMode

                        if (isTracking && settingsChanged) {
                            Toast.makeText(this@MainActivity, "Restarting tracking with new settings...", Toast.LENGTH_SHORT).show()
                            stopTrackerService()
                            // Brief delay to ensure clean stop before restart
                            binding.root.postDelayed({
                                startTrackerService()
                            }, 500)
                        } else {
                            Toast.makeText(this@MainActivity, "Settings saved", Toast.LENGTH_SHORT).show()
                        }
                        dialog.dismiss()
                    }

                    // Check password only if auth fields changed, otherwise save directly
                    if (authFieldsChanged) {
                        Toast.makeText(this@MainActivity, "Checking password...", Toast.LENGTH_SHORT).show()
                        lifecycleScope.launch {
                            val fetcher = EventFetcher()
                            val osVersion = "Android ${android.os.Build.VERSION.RELEASE}"
                            val result = fetcher.checkPassword(
                                serverHost, serverPort, selectedEventId, password,
                                userId = sailorId,
                                userOs = osVersion,
                                userVer = BuildConfig.VERSION_STRING
                            )

                            if (result.isFailure) {
                                val errorMsg = result.exceptionOrNull()?.message ?: "Incorrect password"
                                Toast.makeText(this@MainActivity, errorMsg, Toast.LENGTH_LONG).show()
                                return@launch
                            }

                            saveSettings()
                        }
                    } else {
                        // No auth fields changed, save directly without server check
                        saveSettings()
                    }
                }
            }
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE)?.apply {
                setTextColor(0xFF000000.toInt())
                setBackgroundColor(0xFFCCCCCC.toInt())  // Gray
                textSize = 18f
            }
        }
        dialogRef = dialog
        dialog.show()
    }
    
    // TrackerService.StatusListener implementation
    
    override fun onLocationUpdate(location: Location, totalDistanceMeters: Float) {
        runOnUiThread {
            updateLocationDisplay(location, totalDistanceMeters)
        }
    }
    
    override fun onAckReceived(seq: Int) {
        runOnUiThread {
            binding.tvLastAck.text = "ACK #$seq"
        }
    }
    
    override fun onPacketSent(seq: Int) {
        // Could show send indicator
    }
    
    override fun onConnectionStatus(ackRate: Float) {
        runOnUiThread {
            updateConnectionStatus(ackRate)
        }
    }

    override fun onAuthError(message: String) {
        runOnUiThread {
            // Use Snackbar anchored to assist button so it appears above it, not overlapping stop button
            val snackbar = Snackbar.make(binding.root, "Authentication error: $message", Snackbar.LENGTH_LONG)
            snackbar.anchorView = binding.btnAssist
            snackbar.show()
        }
    }

    override fun onEventName(name: String) {
        runOnUiThread {
            binding.tvEventName.text = if (name.isNotEmpty()) name else "---"
            // Update cached event name and idle screen
            currentEventName = name
            // Save to preferences so it's available on idle screen
            if (name.isNotEmpty()) {
                getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit()
                    .putString("event_name", name)
                    .apply()
            }
            updateIdleScreen()
        }
    }

    override fun onStatusLine(status: String) {
        runOnUiThread {
            binding.tvEventName.text = status
        }
    }

    override fun onAssistEnabled(enabled: Boolean) {
        runOnUiThread {
            binding.btnAssist.visibility = if (enabled) View.VISIBLE else View.GONE
        }
    }
}
