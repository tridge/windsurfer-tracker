package nz.co.tracker.windsurfer.presentation

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
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
    isAssistActive: Boolean,
    speedKnots: Float,
    batteryPercent: Int,
    signalLevel: Int,
    ackRate: Float,
    sailorId: String,
    eventName: String,
    errorMessage: String?,
    highFrequencyMode: Boolean,
    onToggleTracking: () -> Unit,
    onAssistLongPress: () -> Unit,
    onSettingsLongPress: () -> Unit,
    modifier: Modifier = Modifier
) {
    // Pulsing animation for assist mode
    val infiniteTransition = rememberInfiniteTransition(label = "assist_pulse")
    val pulseAlpha by infiniteTransition.animateFloat(
        initialValue = 0.3f,
        targetValue = 0.7f,
        animationSpec = infiniteRepeatable(
            animation = tween(500, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "pulse_alpha"
    )

    // Background color - red pulse when assist active
    val backgroundColor = if (isAssistActive) {
        Color(0xFF880000).copy(alpha = pulseAlpha)
    } else {
        Color.Black
    }

    val statusColor = when {
        isAssistActive -> StoppedRed
        isTracking -> TrackingGreen
        else -> StoppedRed
    }
    val statusText = when {
        isAssistActive -> "⚠ ASSIST ⚠"
        isTracking -> "TRACKING"
        else -> "STOPPED"
    }

    Box(
        modifier = modifier
            .fillMaxSize()
            .background(backgroundColor)
            .pointerInput(Unit) {
                detectTapGestures(
                    onTap = { onToggleTracking() }
                )
            }
    ) {
        // Main content
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
            modifier = Modifier
                .fillMaxSize()
                .padding(horizontal = 16.dp, vertical = 8.dp)
        ) {
            Spacer(modifier = Modifier.height(28.dp))

            // Status indicator
            Text(
                text = statusText,
                color = statusColor,
                fontSize = 14.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center
            )

            // Event name (if available)
            if (eventName.isNotEmpty()) {
                Text(
                    text = eventName,
                    color = Color(0xFF6699FF),
                    fontSize = 10.sp,
                    textAlign = TextAlign.Center,
                    maxLines = 1
                )
            }

            // Sailor ID and 1Hz indicator
            Row(
                horizontalArrangement = Arrangement.Center,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = sailorId,
                    color = Color.Gray,
                    fontSize = 12.sp,
                    textAlign = TextAlign.Center
                )
                if (highFrequencyMode) {
                    Spacer(modifier = Modifier.width(6.dp))
                    Text(
                        text = "1Hz",
                        color = Color.Cyan,
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Bold
                    )
                }
            }

            Spacer(modifier = Modifier.height(4.dp))

            // Speed - large and prominent
            Text(
                text = String.format("%.1f", speedKnots),
                color = Color.White,
                fontSize = 42.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center
            )

            Text(
                text = "kts",
                color = Color.Gray,
                fontSize = 14.sp,
                textAlign = TextAlign.Center
            )

            Spacer(modifier = Modifier.height(4.dp))

            // Status row: Battery, Signal, Connection
            Row(
                horizontalArrangement = Arrangement.spacedBy(12.dp),
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
                    fontSize = 12.sp
                )

                // Signal strength
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
                    fontSize = 10.sp
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
                    fontSize = 12.sp
                )
            }

            Spacer(modifier = Modifier.height(4.dp))

            // Hint text
            Text(
                text = if (isTracking) "Tap to stop" else "Tap to start",
                color = Color.DarkGray,
                fontSize = 10.sp,
                textAlign = TextAlign.Center
            )

            // Error message
            if (!errorMessage.isNullOrEmpty()) {
                Text(
                    text = errorMessage,
                    color = StoppedRed,
                    fontSize = 10.sp,
                    textAlign = TextAlign.Center,
                    maxLines = 2
                )
            }

            Spacer(modifier = Modifier.weight(1f))

            // ASSIST button at bottom
            Box(
                modifier = Modifier
                    .fillMaxWidth(0.85f)
                    .height(36.dp)
                    .clip(MaterialTheme.shapes.small)
                    .background(if (isAssistActive) Color.Red else Color.DarkGray)
                    .pointerInput(Unit) {
                        detectTapGestures(
                            onTap = { onAssistLongPress() }
                        )
                    },
                contentAlignment = Alignment.Center
            ) {
                Text(
                    text = if (isAssistActive) "CANCEL ASSIST" else "ASSIST",
                    color = Color.White,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Bold
                )
            }

            Spacer(modifier = Modifier.height(8.dp))
        }

        // Gear icon - positioned for round watch face, rendered last for touch priority
        Box(
            modifier = Modifier
                .align(Alignment.TopCenter)
                .padding(top = 8.dp)
                .offset(x = 50.dp)  // Offset right from center
                .size(40.dp)
                .clip(CircleShape)
                .pointerInput(Unit) {
                    detectTapGestures(
                        onTap = { onSettingsLongPress() }
                    )
                },
            contentAlignment = Alignment.Center
        ) {
            Text(
                text = "⚙",
                fontSize = 24.sp,
                color = Color.Gray
            )
        }
    }
}
