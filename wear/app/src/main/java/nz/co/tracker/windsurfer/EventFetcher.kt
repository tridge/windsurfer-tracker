package nz.co.tracker.windsurfer

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

data class EventInfo(
    val eid: Int,
    val name: String,
    val description: String
)

class EventFetcher {
    companion object {
        private const val TAG = "EventFetcher"
    }

    suspend fun fetchEvents(serverHost: String, serverPort: Int): List<EventInfo> = withContext(Dispatchers.IO) {
        try {
            // For wstracker.org domains, always use HTTPS on port 443 (nginx proxy)
            val isWstracker = serverHost.contains("wstracker.org")
            val protocol = if (serverPort == 443 || isWstracker) "https" else "http"
            val portSuffix = when {
                isWstracker -> ""  // Always use default HTTPS port for wstracker.org
                protocol == "https" && serverPort == 443 -> ""
                protocol == "http" && serverPort == 80 -> ""
                else -> ":$serverPort"
            }
            val url = URL("$protocol://$serverHost$portSuffix/api/events")

            Log.d(TAG, "Fetching events from: $url")

            val connection = url.openConnection() as HttpURLConnection
            connection.connectTimeout = 10000
            connection.readTimeout = 10000
            connection.requestMethod = "GET"

            val responseCode = connection.responseCode
            if (responseCode != 200) {
                Log.e(TAG, "Failed to fetch events: HTTP $responseCode")
                return@withContext emptyList()
            }

            val response = connection.inputStream.bufferedReader().readText()
            val json = JSONObject(response)
            val eventsArray = json.getJSONArray("events")

            val events = mutableListOf<EventInfo>()
            for (i in 0 until eventsArray.length()) {
                val eventJson = eventsArray.getJSONObject(i)
                events.add(EventInfo(
                    eid = eventJson.getInt("eid"),
                    name = eventJson.getString("name"),
                    description = eventJson.optString("description", "")
                ))
            }

            Log.d(TAG, "Fetched ${events.size} events")
            events
        } catch (e: Exception) {
            Log.e(TAG, "Error fetching events", e)
            emptyList()
        }
    }

    /**
     * Check if the user password is valid for a given event.
     * Uses the tracking endpoint with auth_check flag.
     * Returns a Result indicating success or the error message.
     */
    suspend fun checkPassword(serverHost: String, serverPort: Int, eventId: Int, password: String,
                              userId: String = "unknown", userOs: String = "WearOS", userVer: String = "unknown"): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            // For wstracker.org domains, always use HTTPS on port 443 (nginx proxy)
            val isWstracker = serverHost.contains("wstracker.org")
            val protocol = if (serverPort == 443 || isWstracker) "https" else "http"
            val portSuffix = when {
                isWstracker -> ""
                protocol == "https" && serverPort == 443 -> ""
                protocol == "http" && serverPort == 80 -> ""
                else -> ":$serverPort"
            }
            val url = URL("$protocol://$serverHost$portSuffix/api/tracker")

            Log.d(TAG, "Checking password at: $url")

            val connection = url.openConnection() as HttpURLConnection
            connection.connectTimeout = 10000
            connection.readTimeout = 10000
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.doOutput = true

            // Send auth_check packet (same format as tracking, but with auth_check flag)
            val body = JSONObject().apply {
                put("id", userId)
                put("sq", 1)
                put("ts", System.currentTimeMillis() / 1000)
                put("eid", eventId)
                put("pwd", password)
                put("auth_check", true)
                put("ver", userVer)
                put("os", userOs)
            }.toString()
            connection.outputStream.bufferedWriter().use { it.write(body) }

            val responseCode = connection.responseCode
            val response = try {
                connection.inputStream.bufferedReader().readText()
            } catch (e: Exception) {
                connection.errorStream?.bufferedReader()?.readText() ?: ""
            }

            Log.d(TAG, "Password check response: $responseCode - $response")

            if (responseCode == 200) {
                val json = JSONObject(response)
                // Success if we got an ack and no error
                if (json.has("ack") && !json.has("error")) {
                    Result.success(true)
                } else {
                    Result.failure(Exception(json.optString("msg", "Invalid password")))
                }
            } else if (responseCode == 401) {
                val json = JSONObject(response)
                Result.failure(Exception(json.optString("msg", "Incorrect password")))
            } else if (responseCode == 429) {
                val json = JSONObject(response)
                Result.failure(Exception(json.optString("msg", "Too many attempts, please wait")))
            } else {
                Result.failure(Exception("Server error: $responseCode"))
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error checking password", e)
            Result.failure(Exception("Could not connect to server"))
        }
    }
}
