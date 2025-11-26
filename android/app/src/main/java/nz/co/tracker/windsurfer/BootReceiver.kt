package nz.co.tracker.windsurfer

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Broadcast receiver that restarts tracking after device boot if it was previously active.
 * Technique borrowed from OwnTracks for reliable background tracking.
 */
class BootReceiver : BroadcastReceiver() {
    companion object {
        private const val TAG = "BootReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) {

            Log.d(TAG, "Received ${intent.action}")

            // Check if tracking was active before shutdown/update
            val prefs = context.getSharedPreferences("tracker_prefs", Context.MODE_PRIVATE)
            val wasTracking = prefs.getBoolean("tracking_active", false)

            if (wasTracking) {
                Log.i(TAG, "Restarting tracking after ${intent.action}")

                // Retrieve saved configuration
                val serverHost = prefs.getString("server_host", TrackerService.DEFAULT_SERVER_HOST)
                val serverPort = prefs.getInt("server_port", TrackerService.DEFAULT_SERVER_PORT)
                val sailorId = prefs.getString("sailor_id", "")
                val role = prefs.getString("role", "sailor")
                val password = prefs.getString("password", "")

                // Start the tracking service
                val serviceIntent = Intent(context, TrackerService::class.java).apply {
                    putExtra("server_host", serverHost)
                    putExtra("server_port", serverPort)
                    putExtra("sailor_id", sailorId)
                    putExtra("role", role)
                    putExtra("password", password)
                }

                try {
                    context.startForegroundService(serviceIntent)
                    Log.i(TAG, "Tracking service started successfully")
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to start tracking service", e)
                }
            } else {
                Log.d(TAG, "Tracking was not active, not restarting")
            }
        }
    }
}
