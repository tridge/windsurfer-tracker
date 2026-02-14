package nz.co.tracker.windsurfer

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.content.ComponentName
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.view.KeyEvent
import android.view.accessibility.AccessibilityEvent

/**
 * AccessibilityService that intercepts volume key events to detect simultaneous
 * volume-up + volume-down press for assist toggle.
 *
 * This approach works reliably on all Android versions and devices (Samsung, Pixel,
 * etc.) because onKeyEvent() receives raw key events before system processing,
 * unlike BroadcastReceiver which suffers from Android 14's broadcast coalescing.
 *
 * Enable via app UI prompt, or via adb for fleet setup:
 *   adb shell settings put secure enabled_accessibility_services \
 *     nz.co.tracker.windsurfer/nz.co.tracker.windsurfer.VolumeKeyService
 */
class VolumeKeyService : AccessibilityService() {

    companion object {
        private const val TAG = "VolumeKeyService"
        private const val COMBO_WINDOW_MS = 500L

        /** Callback invoked on the main thread when volume combo is detected. */
        var onVolumeComboDetected: (() -> Unit)? = null

        /** Check if this accessibility service is currently enabled. */
        fun isEnabled(context: Context): Boolean {
            val enabledServices = Settings.Secure.getString(
                context.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
            ) ?: return false
            val componentName = ComponentName(context, VolumeKeyService::class.java).flattenToString()
            return enabledServices.split(':').any { it.equals(componentName, ignoreCase = true) }
        }
    }

    private var lastVolumeUpTime = 0L
    private var lastVolumeDownTime = 0L
    private val mainHandler = Handler(Looper.getMainLooper())

    override fun onServiceConnected() {
        super.onServiceConnected()
        Log.i(TAG, "VolumeKeyService connected")
        serviceInfo = serviceInfo.apply {
            flags = flags or AccessibilityServiceInfo.FLAG_REQUEST_FILTER_KEY_EVENTS
        }
    }

    override fun onKeyEvent(event: KeyEvent): Boolean {
        if (event.action != KeyEvent.ACTION_DOWN) return false

        val now = event.eventTime

        when (event.keyCode) {
            KeyEvent.KEYCODE_VOLUME_UP -> {
                lastVolumeUpTime = now
                if (lastVolumeDownTime > 0 && (now - lastVolumeDownTime) <= COMBO_WINDOW_MS) {
                    triggerCombo()
                }
            }
            KeyEvent.KEYCODE_VOLUME_DOWN -> {
                lastVolumeDownTime = now
                if (lastVolumeUpTime > 0 && (now - lastVolumeUpTime) <= COMBO_WINDOW_MS) {
                    triggerCombo()
                }
            }
        }

        return false  // Don't consume - let volume still change normally
    }

    private fun triggerCombo() {
        lastVolumeUpTime = 0
        lastVolumeDownTime = 0
        Log.i(TAG, "Volume combo detected!")
        mainHandler.post {
            onVolumeComboDetected?.invoke()
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Not used - we only need key event filtering
    }

    override fun onInterrupt() {
        Log.w(TAG, "VolumeKeyService interrupted")
    }

    override fun onDestroy() {
        super.onDestroy()
        Log.i(TAG, "VolumeKeyService destroyed")
    }
}
