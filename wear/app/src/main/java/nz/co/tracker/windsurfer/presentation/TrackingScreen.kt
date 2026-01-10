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
    assistEnabled: Boolean,
    speedKnots: Float,
    distanceMeters: Float,
    batteryPercent: Int,
    signalLevel: Int,
    ackRate: Float,
    lastAckTime: Long,
    sailorId: String,
    eventName: String,
    errorMessage: String?,
    highFrequencyMode: Boolean,
    countdownSeconds: Int?,  // Race countdown timer (null = not active)
    raceTimerEnabled: Boolean,  // Whether to show timer display
    raceTimerMinutes: Int,  // Configured countdown duration
    onToggleTracking: () -> Unit,
    onAssistLongPress: () -> Unit,
    onSettingsLongPress: () -> Unit,
    onTimerStart: () -> Unit,  // Start race countdown
    onTimerReset: () -> Unit,  // Reset race countdown
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

    // ACK-based color coding for TRACKING status
    val statusColor = when {
        isAssistActive -> StoppedRed
        !isTracking -> StoppedRed
        lastAckTime == 0L -> StoppedRed  // No ACK received yet
        else -> {
            val timeSinceAck = System.currentTimeMillis() - lastAckTime
            when {
                timeSinceAck < 30000L -> TrackingGreen  // Green < 30s
                timeSinceAck < 60000L -> Color(0xFFFF8800)  // Orange 30-60s
                else -> StoppedRed  // Red > 60s
            }
        }
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

            // Race timer display or speed
            if (raceTimerEnabled && isTracking) {
                if (countdownSeconds != null) {
                    if (countdownSeconds > 0) {
                        // Countdown running - show remaining time
                        val minutes = countdownSeconds / 60
                        val seconds = countdownSeconds % 60
                        val countdownColor = when {
                            countdownSeconds <= 10 -> StoppedRed
                            countdownSeconds <= 30 -> Color.Yellow
                            else -> Color.Cyan
                        }
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .pointerInput(Unit) {
                                    detectTapGestures(
                                        onTap = { onTimerReset() }
                                    )
                                }
                        ) {
                            Text(
                                text = String.format("%d:%02d", minutes, seconds),
                                color = countdownColor,
                                fontSize = 48.sp,
                                fontWeight = FontWeight.Bold,
                                textAlign = TextAlign.Center
                            )
                            Text(
                                text = "Tap to reset",
                                color = countdownColor,
                                fontSize = 12.sp,
                                textAlign = TextAlign.Center
                            )
                        }
                    } else {
                        // Countdown expired (0:00) - show speed until reset
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .pointerInput(Unit) {
                                    detectTapGestures(
                                        onTap = { onTimerReset() }
                                    )
                                }
                        ) {
                            Row(
                                horizontalArrangement = Arrangement.Center,
                                verticalAlignment = Alignment.Bottom
                            ) {
                                Text(
                                    text = String.format("%.1f", speedKnots),
                                    color = Color.White,
                                    fontSize = 42.sp,
                                    fontWeight = FontWeight.Bold
                                )
                                Spacer(modifier = Modifier.width(4.dp))
                                Text(
                                    text = "kts",
                                    color = Color.Gray,
                                    fontSize = 16.sp,
                                    modifier = Modifier.padding(bottom = 6.dp)
                                )
                            }
                            Text(
                                text = "Tap to reset",
                                color = StoppedRed,
                                fontSize = 12.sp,
                                textAlign = TextAlign.Center
                            )
                        }
                    }
                } else {
                    // Timer enabled but not running - show stopwatch icon + configured time
                    Row(
                        horizontalArrangement = Arrangement.Center,
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.padding(vertical = 8.dp)
                    ) {
                        Text(
                            text = "⏱",
                            fontSize = 40.sp,
                            color = Color.Cyan
                        )
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = String.format("%d:%02d", raceTimerMinutes, 0),
                            color = Color.White,
                            fontSize = 42.sp,
                            fontWeight = FontWeight.Bold
                        )
                    }
                }
            } else {
                // Normal speed display (race timer disabled or not tracking)
                Row(
                    horizontalArrangement = Arrangement.Center,
                    verticalAlignment = Alignment.Bottom
                ) {
                    Text(
                        text = String.format("%.1f", speedKnots),
                        color = Color.White,
                        fontSize = 42.sp,
                        fontWeight = FontWeight.Bold
                    )
                    Spacer(modifier = Modifier.width(4.dp))
                    Text(
                        text = "kts",
                        color = Color.Gray,
                        fontSize = 16.sp,
                        modifier = Modifier.padding(bottom = 6.dp)
                    )
                }

                // Distance in km
                Text(
                    text = String.format("%.1f km", distanceMeters / 1000f),
                    color = Color.Gray,
                    fontSize = 14.sp,
                    textAlign = TextAlign.Center
                )
            }

            // Status row removed per user request - all status shown via color coding of TRACKING/STOPPED text

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

            // ASSIST button at bottom (only show if assist is enabled for this event)
            if (assistEnabled) {
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
