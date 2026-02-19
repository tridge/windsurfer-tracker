package nz.co.tracker.windsurfer.presentation

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.*
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.material.*
import kotlinx.coroutines.delay
import nz.co.tracker.windsurfer.presentation.theme.StoppedRed
import nz.co.tracker.windsurfer.presentation.theme.TrackingGreen
import kotlin.math.roundToInt

@Composable
fun TrackingScreen(
    isTracking: Boolean,
    isIdleMode: Boolean,
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
    // Slide-to-stop confirmation state
    var showStopConfirmation by remember { mutableStateOf(false) }
    // Slide-to-assist confirmation state
    var showAssistConfirmation by remember { mutableStateOf(false) }
    // Brief "stop for settings" message
    var showSettingsBlocked by remember { mutableStateOf(false) }

    // Auto-dismiss after 4 seconds
    LaunchedEffect(showStopConfirmation) {
        if (showStopConfirmation) {
            delay(4000)
            showStopConfirmation = false
        }
    }
    LaunchedEffect(showAssistConfirmation) {
        if (showAssistConfirmation) {
            delay(4000)
            showAssistConfirmation = false
        }
    }
    LaunchedEffect(showSettingsBlocked) {
        if (showSettingsBlocked) {
            delay(2000)
            showSettingsBlocked = false
        }
    }

    // Dismiss if tracking stops externally (remote admin stop)
    LaunchedEffect(isTracking) {
        if (!isTracking) {
            showStopConfirmation = false
            showAssistConfirmation = false
        }
    }

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
        isIdleMode -> Color(0xFF4488FF)  // Blue for idle
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
        isIdleMode -> "IDLE"
        isTracking -> "TRACKING"
        else -> "STOPPED"
    }

    Scaffold(
        timeText = { TimeText() },
        vignette = { Vignette(vignettePosition = VignettePosition.TopAndBottom) }
    ) {
        Box(
            modifier = modifier
                .fillMaxSize()
                .background(backgroundColor)
                .pointerInput(isTracking, isIdleMode) {
                    detectTapGestures(
                        onTap = {
                            if (isTracking && !isIdleMode) {
                                showStopConfirmation = true
                            } else {
                                onToggleTracking()
                            }
                        }
                    )
                }
        ) {
            // Main content
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
                modifier = Modifier
                    .fillMaxSize()
                    .padding(horizontal = 20.dp, vertical = 4.dp)
            ) {
                Spacer(modifier = Modifier.height(24.dp))

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

                // Sailor ID
                Text(
                    text = sailorId,
                    color = Color.Gray,
                    fontSize = 12.sp,
                    textAlign = TextAlign.Center
                )

                Spacer(modifier = Modifier.height(4.dp))

                // Idle mode: show waiting message instead of speed/distance
                if (isIdleMode) {
                    Text(
                        text = "Waiting for\nadmin start",
                        color = Color(0xFF4488FF),
                        fontSize = 16.sp,
                        textAlign = TextAlign.Center,
                        modifier = Modifier.padding(vertical = 8.dp)
                    )
                }

                // Race timer display or speed (only when actively tracking)
                else if (raceTimerEnabled && isTracking) {
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

                // ASSIST button at bottom (only show if assist is enabled and not in idle mode)
                if (assistEnabled && !isIdleMode) {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth(0.75f)
                            .height(36.dp)
                            .clip(MaterialTheme.shapes.small)
                            .background(if (isAssistActive) Color.Red else Color.DarkGray)
                            .pointerInput(isAssistActive) {
                                detectTapGestures(
                                    onTap = {
                                        if (isAssistActive) {
                                            onAssistLongPress() // Cancel immediately
                                        } else {
                                            showAssistConfirmation = true
                                        }
                                    }
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

                Spacer(modifier = Modifier.height(16.dp))
            }

            // Gear icon - positioned for round watch face, rendered last for touch priority
            Box(
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .padding(top = 12.dp)
                    .offset(x = 45.dp)  // Offset right from center
                    .size(40.dp)
                    .clip(CircleShape)
                    .pointerInput(isTracking) {
                        detectTapGestures(
                            onTap = {
                                if (isTracking) {
                                    showSettingsBlocked = true
                                } else {
                                    onSettingsLongPress()
                                }
                            }
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

            // "Stop for settings" brief message
            AnimatedVisibility(
                visible = showSettingsBlocked,
                modifier = Modifier.align(Alignment.Center),
                enter = fadeIn(),
                exit = fadeOut()
            ) {
                Box(
                    modifier = Modifier
                        .background(Color(0xCC000000), RoundedCornerShape(8.dp))
                        .padding(horizontal = 16.dp, vertical = 8.dp)
                ) {
                    Text(
                        text = "Stop for settings",
                        color = Color.White,
                        fontSize = 14.sp,
                        textAlign = TextAlign.Center
                    )
                }
            }

            // Slide-to-stop confirmation overlay
            AnimatedVisibility(
                visible = showStopConfirmation,
                enter = fadeIn(),
                exit = fadeOut()
            ) {
                SlideToConfirmOverlay(
                    title = "Slide to Stop",
                    fillColor = StoppedRed,
                    thumbContent = {
                        Box(
                            modifier = Modifier
                                .size(16.dp)
                                .background(StoppedRed, RoundedCornerShape(2.dp))
                        )
                    },
                    onConfirm = { showStopConfirmation = false; onToggleTracking() },
                    onDismiss = { showStopConfirmation = false }
                )
            }

            // Slide-to-assist confirmation overlay
            AnimatedVisibility(
                visible = showAssistConfirmation,
                enter = fadeIn(),
                exit = fadeOut()
            ) {
                SlideToConfirmOverlay(
                    title = "Slide for Assist",
                    fillColor = Color(0xFFFF8800),
                    thumbContent = {
                        Text(
                            text = "⚠",
                            fontSize = 18.sp
                        )
                    },
                    onConfirm = { showAssistConfirmation = false; onAssistLongPress() },
                    onDismiss = { showAssistConfirmation = false }
                )
            }
        }
    }
}

@Composable
private fun SlideToConfirmOverlay(
    title: String,
    fillColor: Color,
    thumbContent: @Composable () -> Unit,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit
) {
    val haptic = LocalHapticFeedback.current
    val density = LocalDensity.current

    val trackWidthDp = 140.dp
    val thumbSizeDp = 40.dp
    val trackWidthPx = with(density) { trackWidthDp.toPx() }
    val thumbSizePx = with(density) { thumbSizeDp.toPx() }
    val maxDragPx = trackWidthPx - thumbSizePx

    var dragOffsetPx by remember { mutableFloatStateOf(0f) }
    var isDragging by remember { mutableStateOf(false) }
    var hasReachedThreshold by remember { mutableStateOf(false) }

    // Animated snap-back
    val animatedOffset by animateFloatAsState(
        targetValue = if (isDragging) dragOffsetPx else 0f,
        animationSpec = spring(dampingRatio = Spring.DampingRatioMediumBouncy),
        label = "thumb_snapback"
    )
    val displayOffset = if (isDragging) dragOffsetPx else animatedOffset

    val progress = if (maxDragPx > 0f) (displayOffset / maxDragPx).coerceIn(0f, 1f) else 0f
    val threshold = 0.85f

    // Full-screen dark overlay - tap to dismiss
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black.copy(alpha = 0.85f))
            .pointerInput(Unit) {
                detectTapGestures(onTap = { onDismiss() })
            },
        contentAlignment = Alignment.Center
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center
        ) {
            Text(
                text = title,
                color = fillColor,
                fontSize = 16.sp,
                fontWeight = FontWeight.Bold
            )

            Spacer(modifier = Modifier.height(12.dp))

            // Slider track
            Box(
                modifier = Modifier
                    .width(trackWidthDp)
                    .height(thumbSizeDp)
                    .clip(RoundedCornerShape(thumbSizeDp / 2))
                    .background(Color(0xFF333333))
            ) {
                // Color fill following thumb
                Box(
                    modifier = Modifier
                        .width(with(density) { (displayOffset + thumbSizePx).toDp() })
                        .fillMaxHeight()
                        .clip(RoundedCornerShape(thumbSizeDp / 2))
                        .background(
                            if (progress >= threshold) fillColor.copy(alpha = 0.8f)
                            else fillColor.copy(alpha = 0.4f)
                        )
                )

                // Draggable thumb
                Box(
                    modifier = Modifier
                        .offset { IntOffset(displayOffset.roundToInt(), 0) }
                        .size(thumbSizeDp)
                        .clip(CircleShape)
                        .background(Color.White)
                        .pointerInput(Unit) {
                            detectHorizontalDragGestures(
                                onDragStart = {
                                    isDragging = true
                                    hasReachedThreshold = false
                                },
                                onDragEnd = {
                                    isDragging = false
                                    if (dragOffsetPx / maxDragPx >= threshold) {
                                        onConfirm()
                                    }
                                    dragOffsetPx = 0f
                                    hasReachedThreshold = false
                                },
                                onDragCancel = {
                                    isDragging = false
                                    dragOffsetPx = 0f
                                    hasReachedThreshold = false
                                },
                                onHorizontalDrag = { _, dragAmount ->
                                    dragOffsetPx = (dragOffsetPx + dragAmount).coerceIn(0f, maxDragPx)
                                    val currentProgress = dragOffsetPx / maxDragPx
                                    if (currentProgress >= threshold && !hasReachedThreshold) {
                                        hasReachedThreshold = true
                                        haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                                    } else if (currentProgress < threshold) {
                                        hasReachedThreshold = false
                                    }
                                }
                            )
                        },
                    contentAlignment = Alignment.Center
                ) {
                    thumbContent()
                }
            }
        }
    }
}
