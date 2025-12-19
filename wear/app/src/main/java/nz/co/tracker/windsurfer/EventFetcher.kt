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
}
