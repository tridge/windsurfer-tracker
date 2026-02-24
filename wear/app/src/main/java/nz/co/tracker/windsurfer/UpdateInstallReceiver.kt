package nz.co.tracker.windsurfer

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import android.util.Log

/**
 * Receives PackageInstaller session results. When the system needs user confirmation
 * to install the package, it sends a STATUS_PENDING_USER_ACTION with an Intent that
 * must be launched as an Activity to show the confirmation dialog.
 */
class UpdateInstallReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "UpdateInstallReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        val message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: ""

        Log.d(TAG, "Install status: $status, message: $message")

        when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                // System needs user confirmation - launch the confirmation activity
                val confirmIntent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableExtra(Intent.EXTRA_INTENT)
                }
                if (confirmIntent != null) {
                    confirmIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    context.startActivity(confirmIntent)
                }
            }
            PackageInstaller.STATUS_SUCCESS -> {
                Log.d(TAG, "Install succeeded")
            }
            else -> {
                Log.e(TAG, "Install failed: status=$status, message=$message")
            }
        }
    }
}
