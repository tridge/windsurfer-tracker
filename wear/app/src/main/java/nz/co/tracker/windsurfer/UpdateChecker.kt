package nz.co.tracker.windsurfer

import android.content.Context
import android.content.pm.PackageInstaller
import android.os.Build
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL

data class VersionInfo(
    val version: String,
    val versionCode: Int,
    val url: String
)

sealed class UpdateCheckResult {
    data class UpdateAvailable(val versionInfo: VersionInfo) : UpdateCheckResult()
    object NoUpdate : UpdateCheckResult()
    data class Error(val message: String) : UpdateCheckResult()
}

class UpdateChecker(private val context: Context) {

    companion object {
        private const val TAG = "UpdateChecker"
        const val VERSION_URL = "https://wstracker.org/app/version.json"
        const val PREFS_NAME = "update_prefs"
        const val PREF_SKIPPED_VERSION = "skipped_version"
    }

    suspend fun checkForUpdate(): UpdateCheckResult = withContext(Dispatchers.IO) {
        try {
            val response = URL(VERSION_URL).readText()
            val json = JSONObject(response)

            // Read the "wear" sub-object
            val wearJson = json.optJSONObject("wear")
            if (wearJson == null) {
                Log.d(TAG, "No 'wear' section in version.json")
                return@withContext UpdateCheckResult.NoUpdate
            }

            val versionInfo = VersionInfo(
                version = wearJson.getString("version"),
                versionCode = wearJson.getInt("versionCode"),
                url = wearJson.getString("url")
            )

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

    /**
     * Download APK via HttpURLConnection and install via PackageInstaller session API.
     * WearOS has no system package installer UI, so ACTION_VIEW won't work.
     */
    suspend fun downloadAndInstall(
        versionInfo: VersionInfo,
        onProgress: (Int) -> Unit,
        onComplete: () -> Unit,
        onError: (String) -> Unit
    ) = withContext(Dispatchers.IO) {
        try {
            Log.d(TAG, "Downloading APK from ${versionInfo.url}")

            val url = URL(versionInfo.url)
            val connection = url.openConnection() as HttpURLConnection
            connection.connectTimeout = 15000
            connection.readTimeout = 30000
            connection.connect()

            if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                withContext(Dispatchers.Main) {
                    onError("Download failed: HTTP ${connection.responseCode}")
                }
                return@withContext
            }

            val totalSize = connection.contentLength
            val apkFile = File(context.cacheDir, "update-${versionInfo.version}.apk")

            // Download to cache
            connection.inputStream.use { input ->
                apkFile.outputStream().use { output ->
                    val buffer = ByteArray(8192)
                    var downloaded = 0L
                    var bytesRead: Int
                    while (input.read(buffer).also { bytesRead = it } != -1) {
                        output.write(buffer, 0, bytesRead)
                        downloaded += bytesRead
                        if (totalSize > 0) {
                            val percent = (downloaded * 100 / totalSize).toInt()
                            withContext(Dispatchers.Main) { onProgress(percent) }
                        }
                    }
                }
            }

            Log.d(TAG, "Download complete: ${apkFile.length()} bytes")

            // Install via PackageInstaller
            installApk(apkFile)

            withContext(Dispatchers.Main) { onComplete() }

            // Clean up cached file
            apkFile.delete()

        } catch (e: Exception) {
            Log.e(TAG, "Download/install failed", e)
            withContext(Dispatchers.Main) {
                onError(e.message ?: "Download failed")
            }
        }
    }

    private fun installApk(apkFile: File) {
        Log.d(TAG, "Installing APK via PackageInstaller: ${apkFile.absolutePath}")

        val packageInstaller = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(
            PackageInstaller.SessionParams.MODE_FULL_INSTALL
        )
        params.setSize(apkFile.length())

        val sessionId = packageInstaller.createSession(params)
        val session = packageInstaller.openSession(sessionId)

        try {
            // Write APK to session
            session.openWrite("update.apk", 0, apkFile.length()).use { sessionStream ->
                apkFile.inputStream().use { input ->
                    input.copyTo(sessionStream)
                }
                sessionStream.flush()
            }

            // Commit the session with a pending intent for the result
            val intent = android.content.Intent(context, UpdateInstallReceiver::class.java)
            val pendingIntent = android.app.PendingIntent.getBroadcast(
                context,
                sessionId,
                intent,
                android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_MUTABLE
            )
            session.commit(pendingIntent.intentSender)
            Log.d(TAG, "PackageInstaller session committed")
        } catch (e: Exception) {
            session.abandon()
            throw e
        }
    }

    fun getCurrentVersionString(): String {
        return try {
            val pInfo = context.packageManager.getPackageInfo(context.packageName, 0)
            val versionName = pInfo.versionName ?: "unknown"
            val versionCode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                pInfo.longVersionCode
            } else {
                @Suppress("DEPRECATION")
                pInfo.versionCode.toLong()
            }
            "$versionName ($versionCode) ${BuildConfig.GIT_HASH}"
        } catch (e: Exception) {
            "unknown"
        }
    }
}
