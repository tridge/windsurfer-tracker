package nz.co.tracker.windsurfer.presentation.theme

import androidx.compose.ui.graphics.Color
import androidx.wear.compose.material.Colors
import androidx.wear.compose.material.MaterialTheme
import androidx.compose.runtime.Composable

// High-contrast colors for outdoor visibility
val TrackingGreen = Color(0xFF00E676)  // Bright green
val StoppedRed = Color(0xFFFF5252)     // Bright red
val PrimaryBlue = Color(0xFF2196F3)   // Blue for accents
val SurfaceGray = Color(0xFF1A1A1A)   // Dark background
val OnSurfaceWhite = Color(0xFFFFFFFF)

internal val wearColorPalette: Colors = Colors(
    primary = PrimaryBlue,
    primaryVariant = Color(0xFF1565C0),
    secondary = TrackingGreen,
    secondaryVariant = Color(0xFF00C853),
    background = Color.Black,
    surface = SurfaceGray,
    error = StoppedRed,
    onPrimary = Color.White,
    onSecondary = Color.Black,
    onBackground = Color.White,
    onSurface = OnSurfaceWhite,
    onError = Color.White
)

@Composable
fun WindsurferTrackerTheme(
    content: @Composable () -> Unit
) {
    MaterialTheme(
        colors = wearColorPalette,
        content = content
    )
}
