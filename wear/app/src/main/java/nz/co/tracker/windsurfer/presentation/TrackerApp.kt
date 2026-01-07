package nz.co.tracker.windsurfer.presentation

import androidx.compose.runtime.*
import androidx.wear.compose.navigation.SwipeDismissableNavHost
import androidx.wear.compose.navigation.composable
import androidx.wear.compose.navigation.rememberSwipeDismissableNavController
import nz.co.tracker.windsurfer.TrackerSettings
import nz.co.tracker.windsurfer.presentation.theme.WindsurferTrackerTheme

sealed class Screen(val route: String) {
    object Tracking : Screen("tracking")
    object Settings : Screen("settings")
}

@Composable
fun TrackerApp(
    isTracking: Boolean,
    isAssistActive: Boolean,
    assistEnabled: Boolean,
    speedKnots: Float,
    distanceMeters: Float,
    batteryPercent: Int,
    signalLevel: Int,
    ackRate: Float,
    eventName: String,
    errorMessage: String?,
    settings: TrackerSettings,
    countdownSeconds: Int?,  // Race countdown timer (null = not active)
    onToggleTracking: () -> Unit,
    onAssistToggle: () -> Unit,
    onSaveSettings: (TrackerSettings) -> Unit,
    onTimerStart: () -> Unit,
    onTimerReset: () -> Unit
) {
    WindsurferTrackerTheme {
        val navController = rememberSwipeDismissableNavController()

        // Start at settings if ID or password is missing
        val needsSetup = settings.sailorId.isEmpty() || settings.password.isEmpty()
        val startRoute = if (needsSetup) Screen.Settings.route else Screen.Tracking.route

        SwipeDismissableNavHost(
            navController = navController,
            startDestination = startRoute
        ) {
            composable(Screen.Tracking.route) {
                TrackingScreen(
                    isTracking = isTracking,
                    isAssistActive = isAssistActive,
                    assistEnabled = assistEnabled,
                    speedKnots = speedKnots,
                    distanceMeters = distanceMeters,
                    batteryPercent = batteryPercent,
                    signalLevel = signalLevel,
                    ackRate = ackRate,
                    sailorId = settings.sailorId,
                    eventName = eventName,
                    errorMessage = errorMessage,
                    highFrequencyMode = settings.highFrequencyMode,
                    countdownSeconds = countdownSeconds,
                    raceTimerEnabled = settings.raceTimerEnabled,
                    onToggleTracking = onToggleTracking,
                    onAssistLongPress = onAssistToggle,
                    onSettingsLongPress = {
                        navController.navigate(Screen.Settings.route)
                    },
                    onTimerStart = onTimerStart,
                    onTimerReset = onTimerReset
                )
            }

            composable(Screen.Settings.route) {
                SettingsScreen(
                    settings = settings,
                    onSave = onSaveSettings,
                    onBack = {
                        navController.popBackStack()
                    }
                )
            }
        }
    }
}
