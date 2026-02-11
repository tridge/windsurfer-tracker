package nz.co.tracker.windsurfer

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Broadcast receiver for configuring tracker settings via ADB.
 *
 * Usage:
 *   adb shell am broadcast -a nz.co.tracker.windsurfer.CONFIGURE \
 *     --es sailor_id "Tracker11" \
 *     --ei event_id 1 \
 *     --es password "xyz123" \
 *     nz.co.tracker.windsurfer/.ConfigReceiver
 *
 * Supported extras:
 *   sailor_id (String) - tracker user ID
 *   event_id (int) - event ID
 *   password (String) - event password
 *   auto_start_on_boot (boolean) - enable auto-start on boot (default: true when configuring)
 *   role (String) - "sailor", "support", or "spectator"
 *   server_host (String) - server hostname
 *   server_port (int) - server port
 */
class ConfigReceiver : BroadcastReceiver() {
    companion object {
        private const val TAG = "ConfigReceiver"
        const val ACTION_CONFIGURE = "nz.co.tracker.windsurfer.CONFIGURE"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_CONFIGURE) return

        val deviceContext = context.createDeviceProtectedStorageContext()
        val prefs = deviceContext.getSharedPreferences("tracker_prefs", Context.MODE_PRIVATE)
        val editor = prefs.edit()
        var count = 0

        intent.getStringExtra("sailor_id")?.let {
            editor.putString("sailor_id", it)
            count++
        }
        if (intent.hasExtra("event_id")) {
            editor.putInt("event_id", intent.getIntExtra("event_id", 2))
            count++
        }
        intent.getStringExtra("password")?.let {
            editor.putString("password", it)
            // Also save per-event password
            val eventId = if (intent.hasExtra("event_id")) {
                intent.getIntExtra("event_id", 2)
            } else {
                prefs.getInt("event_id", 2)
            }
            editor.putString("event_password_$eventId", it)
            count++
        }
        if (intent.hasExtra("auto_start_on_boot")) {
            editor.putBoolean("auto_start_on_boot", intent.getBooleanExtra("auto_start_on_boot", true))
            count++
        }
        intent.getStringExtra("role")?.let {
            editor.putString("role", it)
            count++
        }
        intent.getStringExtra("server_host")?.let {
            editor.putString("server_host", it)
            count++
        }
        if (intent.hasExtra("server_port")) {
            editor.putInt("server_port", intent.getIntExtra("server_port", TrackerService.DEFAULT_SERVER_PORT))
            count++
        }

        editor.commit()
        Log.i(TAG, "Configured $count settings via ADB")
    }
}
