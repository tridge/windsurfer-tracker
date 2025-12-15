package nz.co.tracker.windsurfer.presentation

import android.location.Location
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.material.*
import nz.co.tracker.windsurfer.presentation.theme.StoppedRed
import nz.co.tracker.windsurfer.presentation.theme.TrackingGreen

@Composable
fun TrackingScreen(
    isTracking: Boolean,
    speedKnots: Float,
    batteryPercent: Int,
    signalLevel: Int,
    ackRate: Float,
    sailorId: String,
    onToggleTracking: () -> Unit,
    onLongPress: () -> Unit,
    modifier: Modifier = Modifier
) {
    val statusColor = if (isTracking) TrackingGreen else StoppedRed
    val statusText = if (isTracking) "TRACKING" else "STOPPED"

    Box(
        modifier = modifier
            .fillMaxSize()
            .background(Color.Black)
            .pointerInput(Unit) {
                detectTapGestures(
                    onTap = { onToggleTracking() },
                    onLongPress = { onLongPress() }
                )
            },
        contentAlignment = Alignment.Center
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
            modifier = Modifier
                .fillMaxSize()
                .padding(16.dp)
        ) {
            // Status indicator at top
            Text(
                text = statusText,
                color = statusColor,
                fontSize = 16.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center
            )

            Spacer(modifier = Modifier.height(4.dp))

            // Sailor ID
            Text(
                text = sailorId,
                color = Color.Gray,
                fontSize = 14.sp,
                textAlign = TextAlign.Center
            )

            Spacer(modifier = Modifier.height(8.dp))

            // Speed - large and prominent
            Text(
                text = String.format("%.1f", speedKnots),
                color = Color.White,
                fontSize = 48.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center
            )

            Text(
                text = "kts",
                color = Color.Gray,
                fontSize = 18.sp,
                textAlign = TextAlign.Center
            )

            Spacer(modifier = Modifier.height(12.dp))

            // Bottom row: Battery and Signal
            Row(
                horizontalArrangement = Arrangement.spacedBy(16.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                // Battery
                val batteryColor = when {
                    batteryPercent > 50 -> TrackingGreen
                    batteryPercent > 20 -> Color.Yellow
                    else -> StoppedRed
                }
                Text(
                    text = "$batteryPercent%",
                    color = batteryColor,
                    fontSize = 14.sp
                )

                // Signal strength (0-4 bars)
                val signalText = when (signalLevel) {
                    -1 -> "---"
                    0 -> "▁"
                    1 -> "▁▂"
                    2 -> "▁▂▃"
                    3 -> "▁▂▃▄"
                    4 -> "▁▂▃▄▅"
                    else -> "▁▂▃▄▅"
                }
                Text(
                    text = signalText,
                    color = if (signalLevel >= 2) TrackingGreen else Color.Yellow,
                    fontSize = 12.sp
                )

                // Connection indicator
                val connColor = when {
                    ackRate > 0.8f -> TrackingGreen
                    ackRate > 0.4f -> Color.Yellow
                    else -> StoppedRed
                }
                Text(
                    text = if (ackRate > 0) "●" else "○",
                    color = connColor,
                    fontSize = 14.sp
                )
            }

            Spacer(modifier = Modifier.height(8.dp))

            // Hint text
            Text(
                text = if (isTracking) "Tap to stop" else "Tap to start",
                color = Color.DarkGray,
                fontSize = 12.sp,
                textAlign = TextAlign.Center
            )

            Text(
                text = "Long press: Settings",
                color = Color.DarkGray,
                fontSize = 10.sp,
                textAlign = TextAlign.Center
            )
        }
    }
}
