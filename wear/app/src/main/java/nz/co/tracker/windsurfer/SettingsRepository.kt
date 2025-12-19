package nz.co.tracker.windsurfer

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "settings")

data class TrackerSettings(
    val serverHost: String = TrackerService.DEFAULT_SERVER_HOST,
    val sailorId: String = "W01",  // W for Watch
    val role: String = "sailor",
    val password: String = "",
    val eventId: Int = 1,  // Event ID for multi-event support
    val highFrequencyMode: Boolean = false  // 1Hz mode for racing
)

class SettingsRepository(private val context: Context) {

    private object PreferencesKeys {
        val SERVER_HOST = stringPreferencesKey("server_host")
        val SAILOR_ID = stringPreferencesKey("sailor_id")
        val ROLE = stringPreferencesKey("role")
        val PASSWORD = stringPreferencesKey("password")
        val EVENT_ID = intPreferencesKey("event_id")
        val HIGH_FREQUENCY_MODE = booleanPreferencesKey("high_frequency_mode")
    }

    val settingsFlow: Flow<TrackerSettings> = context.dataStore.data.map { prefs ->
        TrackerSettings(
            serverHost = prefs[PreferencesKeys.SERVER_HOST] ?: TrackerService.DEFAULT_SERVER_HOST,
            sailorId = prefs[PreferencesKeys.SAILOR_ID] ?: "W01",
            role = prefs[PreferencesKeys.ROLE] ?: "sailor",
            password = prefs[PreferencesKeys.PASSWORD] ?: "",
            eventId = prefs[PreferencesKeys.EVENT_ID] ?: 1,
            highFrequencyMode = prefs[PreferencesKeys.HIGH_FREQUENCY_MODE] ?: false
        )
    }

    suspend fun updateSettings(settings: TrackerSettings) {
        context.dataStore.edit { prefs ->
            prefs[PreferencesKeys.SERVER_HOST] = settings.serverHost
            prefs[PreferencesKeys.SAILOR_ID] = settings.sailorId
            prefs[PreferencesKeys.ROLE] = settings.role
            prefs[PreferencesKeys.PASSWORD] = settings.password
            prefs[PreferencesKeys.EVENT_ID] = settings.eventId
            prefs[PreferencesKeys.HIGH_FREQUENCY_MODE] = settings.highFrequencyMode
        }
    }

    suspend fun updateServerHost(host: String) {
        context.dataStore.edit { prefs ->
            prefs[PreferencesKeys.SERVER_HOST] = host
        }
    }

    suspend fun updateSailorId(id: String) {
        context.dataStore.edit { prefs ->
            prefs[PreferencesKeys.SAILOR_ID] = id
        }
    }

    suspend fun updateRole(role: String) {
        context.dataStore.edit { prefs ->
            prefs[PreferencesKeys.ROLE] = role
        }
    }
}
