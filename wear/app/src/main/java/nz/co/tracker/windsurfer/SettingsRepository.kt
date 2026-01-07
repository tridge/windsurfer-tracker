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
    val eventId: Int = 2,  // Event ID for multi-event support
    val highFrequencyMode: Boolean = false,  // 1Hz mode for racing
    val heartRateEnabled: Boolean = false,  // Opt-in for health data privacy
    val trackerBeep: Boolean = true,  // Beep once per minute to remind user tracker is running
    val raceTimerEnabled: Boolean = false,  // Race countdown timer
    val raceTimerMinutes: Int = 5  // Countdown duration (1-9 minutes)
)

class SettingsRepository(private val context: Context) {

    private object PreferencesKeys {
        val SERVER_HOST = stringPreferencesKey("server_host")
        val SAILOR_ID = stringPreferencesKey("sailor_id")
        val ROLE = stringPreferencesKey("role")
        val PASSWORD = stringPreferencesKey("password")
        val EVENT_ID = intPreferencesKey("event_id")
        val HIGH_FREQUENCY_MODE = booleanPreferencesKey("high_frequency_mode")
        val HEART_RATE_ENABLED = booleanPreferencesKey("heart_rate_enabled")
        val TRACKER_BEEP = booleanPreferencesKey("tracker_beep")
        val RACE_TIMER_ENABLED = booleanPreferencesKey("race_timer_enabled")
        val RACE_TIMER_MINUTES = intPreferencesKey("race_timer_minutes")
    }

    val settingsFlow: Flow<TrackerSettings> = context.dataStore.data.map { prefs ->
        TrackerSettings(
            serverHost = prefs[PreferencesKeys.SERVER_HOST] ?: TrackerService.DEFAULT_SERVER_HOST,
            sailorId = prefs[PreferencesKeys.SAILOR_ID] ?: "W01",
            role = prefs[PreferencesKeys.ROLE] ?: "sailor",
            password = prefs[PreferencesKeys.PASSWORD] ?: "",
            eventId = prefs[PreferencesKeys.EVENT_ID] ?: 2,
            highFrequencyMode = prefs[PreferencesKeys.HIGH_FREQUENCY_MODE] ?: false,
            heartRateEnabled = prefs[PreferencesKeys.HEART_RATE_ENABLED] ?: false,
            trackerBeep = prefs[PreferencesKeys.TRACKER_BEEP] ?: true,
            raceTimerEnabled = prefs[PreferencesKeys.RACE_TIMER_ENABLED] ?: false,
            raceTimerMinutes = prefs[PreferencesKeys.RACE_TIMER_MINUTES] ?: 5
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
            prefs[PreferencesKeys.HEART_RATE_ENABLED] = settings.heartRateEnabled
            prefs[PreferencesKeys.TRACKER_BEEP] = settings.trackerBeep
            prefs[PreferencesKeys.RACE_TIMER_ENABLED] = settings.raceTimerEnabled
            prefs[PreferencesKeys.RACE_TIMER_MINUTES] = settings.raceTimerMinutes
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
