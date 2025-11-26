package nz.co.tracker.windsurfer

import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.util.Log
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.net.URL

data class VersionInfo(
    val version: String,
    val versionCode: Int,
    val url: String,
    val changelog: String
)

sealed class UpdateCheckResult {
    data class UpdateAvailable(val versionInfo: VersionInfo) : UpdateCheckResult()
    object NoUpdate : UpdateCheckResult()
    data class Error(val message: String) : UpdateCheckResult()
}

class UpdateChecker(private val context: Context) {

    companion object {
        private const val TAG = "UpdateChecker"
        const val VERSION_URL = "https://track.tridgell.net/app/version.json"
        const val PREFS_NAME = "update_prefs"
        const val PREF_SKIPPED_VERSION = "skipped_version"
    }

    suspend fun checkForUpdate(): UpdateCheckResult = withContext(Dispatchers.IO) {
        try {
            val response = URL(VERSION_URL).readText()
            val json = JSONObject(response)

            val versionInfo = VersionInfo(
                version = json.getString("version"),
                versionCode = json.getInt("versionCode"),
                url = json.getString("url"),
                changelog = json.optString("changelog", "")
            )

            // Compare with current version
            val currentVersionCode = try {
                val pInfo = context.packageManager.getPackageInfo(context.packageName, 0)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    pInfo.longVersionCode.toInt()
                } else {
                    @Suppress("DEPRECATION")
                    pInfo.versionCode
                }
            } catch (e: Exception) {
                Log.e(TAG, "Could not get current version", e)
                0
            }

            Log.d(TAG, "Current versionCode: $currentVersionCode, server versionCode: ${versionInfo.versionCode}")

            if (versionInfo.versionCode > currentVersionCode) {
                UpdateCheckResult.UpdateAvailable(versionInfo)
            } else {
                UpdateCheckResult.NoUpdate
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to check for updates", e)
            UpdateCheckResult.Error(e.message ?: "Failed to check for updates")
        }
    }

    fun isVersionSkipped(versionCode: Int): Boolean {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getInt(PREF_SKIPPED_VERSION, 0) == versionCode
    }

    fun skipVersion(versionCode: Int) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().putInt(PREF_SKIPPED_VERSION, versionCode).apply()
    }

    fun clearSkippedVersion() {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().remove(PREF_SKIPPED_VERSION).apply()
    }

    fun downloadAndInstall(versionInfo: VersionInfo, onProgress: (Int) -> Unit, onComplete: () -> Unit, onError: (String) -> Unit) {
        try {
            // Create updates directory
            val updatesDir = File(context.getExternalFilesDir(null), "updates")
            if (!updatesDir.exists()) {
                updatesDir.mkdirs()
            }

            // Delete old APKs
            updatesDir.listFiles()?.forEach { it.delete() }

            val apkFile = File(updatesDir, "tracker-${versionInfo.version}.apk")

            val request = DownloadManager.Request(Uri.parse(versionInfo.url))
                .setTitle("Windsurfer Tracker Update")
                .setDescription("Downloading version ${versionInfo.version}")
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE)
                .setDestinationUri(Uri.fromFile(apkFile))
                .setAllowedOverMetered(true)
                .setAllowedOverRoaming(true)

            val downloadManager = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
            val downloadId = downloadManager.enqueue(request)

            // Register receiver for download completion
            val receiver = object : BroadcastReceiver() {
                override fun onReceive(ctx: Context?, intent: Intent?) {
                    Log.d(TAG, "Received broadcast: ${intent?.action}")
                    val id = intent?.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1)
                    Log.d(TAG, "Download ID from broadcast: $id, expected: $downloadId")
                    if (id == downloadId) {
                        context.unregisterReceiver(this)

                        // Check download status
                        val query = DownloadManager.Query().setFilterById(downloadId)
                        val cursor = downloadManager.query(query)
                        if (cursor.moveToFirst()) {
                            val statusIndex = cursor.getColumnIndex(DownloadManager.COLUMN_STATUS)
                            val status = cursor.getInt(statusIndex)
                            Log.d(TAG, "Download status: $status")
                            if (status == DownloadManager.STATUS_SUCCESSFUL) {
                                Log.d(TAG, "Download successful, APK at: ${apkFile.absolutePath}, exists: ${apkFile.exists()}")
                                onComplete()
                                installApk(apkFile)
                            } else {
                                val reasonIndex = cursor.getColumnIndex(DownloadManager.COLUMN_REASON)
                                val reason = cursor.getInt(reasonIndex)
                                Log.e(TAG, "Download failed with status $status, reason $reason")
                                onError("Download failed (status: $status, reason: $reason)")
                            }
                        }
                        cursor.close()
                    }
                }
            }

            // Use RECEIVER_EXPORTED for system broadcasts like DOWNLOAD_COMPLETE
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                context.registerReceiver(
                    receiver,
                    IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE),
                    Context.RECEIVER_EXPORTED
                )
            } else {
                context.registerReceiver(
                    receiver,
                    IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE)
                )
            }

            Log.d(TAG, "Download started with ID: $downloadId")

        } catch (e: Exception) {
            Log.e(TAG, "Download failed", e)
            onError(e.message ?: "Download failed")
        }
    }

    private fun installApk(apkFile: File) {
        try {
            Log.d(TAG, "Installing APK from: ${apkFile.absolutePath}")
            val uri = FileProvider.getUriForFile(
                context,
                "${context.packageName}.fileprovider",
                apkFile
            )
            Log.d(TAG, "FileProvider URI: $uri")

            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION
            }

            Log.d(TAG, "Starting install activity")
            context.startActivity(intent)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to install APK", e)
        }
    }

    fun getCurrentVersionString(): String {
        return try {
            val pInfo = context.packageManager.getPackageInfo(context.packageName, 0)
            pInfo.versionName ?: "unknown"
        } catch (e: Exception) {
            "unknown"
        }
    }
}
