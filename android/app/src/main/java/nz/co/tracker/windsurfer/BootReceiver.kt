package nz.co.tracker.windsurfer

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

/**
 * Broadcast receiver that auto-starts tracking after device boot if enabled in settings.
 * Also handles shutdown to send stop packet before device powers off.
 * Supports Direct Boot (LOCKED_BOOT_COMPLETED) for starting before user unlocks.
 */
class BootReceiver : BroadcastReceiver() {
    companion object {
        private const val TAG = "BootReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        Log.d(TAG, "Received ${intent.action}")

        // Handle shutdown - send stop packet if tracking was active
        if (intent.action == Intent.ACTION_SHUTDOWN ||
            intent.action == "android.intent.action.QUICKBOOT_POWEROFF") {
            handleShutdown(context)
            return
        }

        // Handle boot
        if (intent.action == Intent.ACTION_LOCKED_BOOT_COMPLETED ||
            intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) {

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
                val highFrequencyMode = prefs.getBoolean("high_frequency_mode", true)

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

    /**
     * Handle device shutdown by sending a stop packet if tracking was active.
     * Sends minimal packet without position (server accepts this for stop messages).
     */
    private fun handleShutdown(context: Context) {
        val deviceContext = context.createDeviceProtectedStorageContext()
        val prefs = deviceContext.getSharedPreferences("tracker_prefs", Context.MODE_PRIVATE)

        // Check if tracking was active
        val wasTracking = prefs.getBoolean("tracking_active", false)
        if (!wasTracking) {
            Log.d(TAG, "Tracking was not active, no stop packet needed")
            return
        }

        val serverHost = prefs.getString("server_host", TrackerService.DEFAULT_SERVER_HOST) ?: TrackerService.DEFAULT_SERVER_HOST
        val serverPort = prefs.getInt("server_port", TrackerService.DEFAULT_SERVER_PORT)
        val sailorId = prefs.getString("sailor_id", "") ?: ""
        val password = prefs.getString("password", "") ?: ""
        val eventId = prefs.getInt("event_id", 2)

        if (sailorId.isEmpty()) {
            Log.w(TAG, "No sailor ID, cannot send stop packet")
            return
        }

        Log.i(TAG, "Sending shutdown stop packet for $sailorId")

        // Send stop packet in a thread and wait for it (goAsync lets us run longer)
        val pendingResult = goAsync()
        Thread {
            try {
                val packet = JSONObject().apply {
                    put("id", sailorId)
                    put("eid", eventId)
                    put("sq", System.currentTimeMillis() / 1000)  // Use timestamp as seq
                    put("ts", System.currentTimeMillis() / 1000)
                    put("stopped", true)
                    put("ver", BuildConfig.VERSION_STRING)
                    if (password.isNotEmpty()) {
                        put("pwd", password)
                    }
                }

                val data = packet.toString().toByteArray(Charsets.UTF_8)
                val address = InetAddress.getByName(serverHost)
                val socket = DatagramSocket()
                socket.soTimeout = 1000  // 1 second timeout
                val dgram = DatagramPacket(data, data.size, address, serverPort)
                socket.send(dgram)
                socket.close()

                Log.i(TAG, "Shutdown stop packet sent successfully")

                // Clear tracking state
                prefs.edit().putBoolean("tracking_active", false).apply()
            } catch (e: Exception) {
                Log.e(TAG, "Failed to send shutdown stop packet", e)
            } finally {
                pendingResult.finish()
            }
        }.start()
    }
}
