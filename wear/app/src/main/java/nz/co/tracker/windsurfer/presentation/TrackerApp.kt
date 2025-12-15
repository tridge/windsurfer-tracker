package nz.co.tracker.windsurfer.presentation

import android.location.Location
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
    speedKnots: Float,
    batteryPercent: Int,
    signalLevel: Int,
    ackRate: Float,
    settings: TrackerSettings,
    onToggleTracking: () -> Unit,
    onSaveSettings: (TrackerSettings) -> Unit
) {
    WindsurferTrackerTheme {
        val navController = rememberSwipeDismissableNavController()

        SwipeDismissableNavHost(
            navController = navController,
            startDestination = Screen.Tracking.route
        ) {
            composable(Screen.Tracking.route) {
                TrackingScreen(
                    isTracking = isTracking,
                    speedKnots = speedKnots,
                    batteryPercent = batteryPercent,
                    signalLevel = signalLevel,
                    ackRate = ackRate,
                    sailorId = settings.sailorId,
                    onToggleTracking = onToggleTracking,
                    onLongPress = {
                        navController.navigate(Screen.Settings.route)
                    }
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
