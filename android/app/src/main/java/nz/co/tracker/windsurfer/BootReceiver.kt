package nz.co.tracker.windsurfer

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Broadcast receiver that auto-starts tracking after device boot if enabled in settings.
 * Supports Direct Boot (LOCKED_BOOT_COMPLETED) for starting before user unlocks.
 * Technique borrowed from OwnTracks for reliable background tracking.
 */
class BootReceiver : BroadcastReceiver() {
    companion object {
        private const val TAG = "BootReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_LOCKED_BOOT_COMPLETED ||
            intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) {

            Log.d(TAG, "Received ${intent.action}")

            // Use device-protected storage for Direct Boot support
            // This storage is available before user unlocks the device
            val deviceContext = context.createDeviceProtectedStorageContext()
            val prefs = deviceContext.getSharedPreferences("tracker_prefs", Context.MODE_PRIVATE)
            val autoStartEnabled = prefs.getBoolean("auto_start_on_boot", false)

            if (autoStartEnabled) {
                // Retrieve saved configuration
                val serverHost = prefs.getString("server_host", TrackerService.DEFAULT_SERVER_HOST)
                val serverPort = prefs.getInt("server_port", TrackerService.DEFAULT_SERVER_PORT)
                val sailorId = prefs.getString("sailor_id", "") ?: ""
                val role = prefs.getString("role", "sailor")
                val password = prefs.getString("password", "") ?: ""
                val highFrequencyMode = prefs.getBoolean("high_frequency_mode", false)

                // Don't start if sailorId or password is empty
                if (sailorId.isEmpty() || password.isEmpty()) {
                    Log.w(TAG, "Not starting tracking: sailorId or password is empty")
                    return
                }

                Log.i(TAG, "Auto-starting tracking after ${intent.action}")

                // Mark tracking as active so UI shows correct state when opened
                prefs.edit().putBoolean("tracking_active", true).apply()

                // Start the tracking service
                val serviceIntent = Intent(context, TrackerService::class.java).apply {
                    putExtra("server_host", serverHost)
                    putExtra("server_port", serverPort)
                    putExtra("sailor_id", sailorId)
                    putExtra("role", role)
                    putExtra("password", password)
                    putExtra("high_frequency_mode", highFrequencyMode)
                }

                try {
                    context.startForegroundService(serviceIntent)
                    Log.i(TAG, "Tracking service started successfully")
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to start tracking service", e)
                }
            } else {
                Log.d(TAG, "Auto-start on boot not enabled, not starting")
            }
        }
    }
}
