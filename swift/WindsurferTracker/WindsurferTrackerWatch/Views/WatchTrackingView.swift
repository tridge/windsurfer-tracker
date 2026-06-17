import SwiftUI
import WatchKit

/// Compact tracking display for watch - matches WearOS design
struct WatchTrackingView: View {
    @EnvironmentObject var viewModel: WatchTrackerViewModel

    // Throttled heart rate display (updates at most once per second)
    @State private var displayedHeartRate: Int = 0
    @State private var lastHeartRateUpdate: Date = .distantPast

    // Slide-to-confirm overlay state
    @State private var showStopConfirmation = false
    @State private var showAssistConfirmation = false
    @State private var showSettingsBlocked = false
    @State private var navigateToSettings = false

    var body: some View {
        ZStack {
            // Red background when assist is active
            if viewModel.assistRequested {
                Color(red: 0.3, green: 0.05, blue: 0.05)
                    .ignoresSafeArea()
            }

            VStack(spacing: 4) {
                // Header with settings gear - top left to avoid clock
                HStack {
                    // Hidden NavigationLink for programmatic navigation
                    NavigationLink(isActive: $navigateToSettings) {
                        WatchSettingsView()
                            .environmentObject(viewModel)
                    } label: {
                        EmptyView()
                    }
                    .hidden()
                    .frame(width: 0, height: 0)

                    Button {
                        if viewModel.isTracking {
                            showSettingsBlocked = true
                        } else {
                            navigateToSettings = true
                        }
                    } label: {
                        Image(systemName: "gearshape")
                            .font(.body)
                            .foregroundColor(.gray)
                    }
                    .buttonStyle(.plain)
                    Spacer()
                }
                .padding(.leading, 24)
                .padding(.top, 16)

                // Status title with ACK-based color coding
                if viewModel.assistRequested {
                    HStack(spacing: 4) {
                        Text("⚠")
                            .foregroundColor(.red)
                        Text("ASSIST")
                            .bold()
                            .foregroundColor(.red)
                        Text("⚠")
                            .foregroundColor(.red)
                    }
                    .font(.caption)
                } else if viewModel.isIdleMode {
                    Text("IDLE")
                        .font(.caption)
                        .bold()
                        .foregroundColor(.blue)
                } else {
                    Text(WKInterfaceDevice.current().isWaterLockEnabled ? "TRACKING(LOCKED)" : "TRACKING")
                        .font(.caption)
                        .bold()
                        .foregroundColor(trackingStatusColor)
                }

                // Status line (GPS wait, connecting, auth failure, or event name)
                Text(viewModel.statusLine)
                    .font(.caption2)
                    .foregroundColor(viewModel.statusLine == "auth failure" ? .red : .blue)
                    .lineLimit(1)

                // Sailor ID
                Text(viewModel.sailorId)
                    .font(.caption2)
                    .foregroundColor(.white)

                // Idle mode: show waiting message and Start button
                if viewModel.isIdleMode {
                    Text("Waiting for\nadmin start")
                        .font(.caption)
                        .foregroundColor(.blue)
                        .multilineTextAlignment(.center)
                        .padding(.vertical, 8)

                    Button { viewModel.startTracking() } label: {
                        HStack {
                            Image(systemName: "play.fill")
                            Text("Start").bold()
                        }
                        .font(.body)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                        .background(Color.green)
                        .foregroundColor(.black)
                        .cornerRadius(24)
                    }
                    .buttonStyle(.plain)
                    .padding(.horizontal, 16)
                }
                // Show countdown when active, otherwise show speed or stopwatch
                // Race timer feature is disabled for this release
                else if RACE_TIMER_FEATURE_ENABLED, let countdown = viewModel.countdownSeconds {
                    // Race countdown timer display
                    VStack(spacing: 4) {
                        if countdown > 0 {
                            // Timer running - show remaining time
                            let minutes = countdown / 60
                            let seconds = countdown % 60
                            let countdownColor: Color = {
                                if countdown <= 10 { return .red }
                                if countdown <= 30 { return .yellow }
                                return .cyan
                            }()

                            Text(String(format: "%d:%02d", minutes, seconds))
                                .font(.system(size: 42, weight: .bold, design: .rounded))
                                .foregroundColor(countdownColor)
                        } else {
                            // Timer expired - show speed until reset
                            HStack(alignment: .lastTextBaseline, spacing: 2) {
                                Text(speedText)
                                    .font(.system(size: 36, weight: .bold, design: .rounded))
                                    .foregroundColor(.white)
                                Text("kts")
                                    .font(.caption)
                                    .foregroundColor(.gray)
                            }
                        }

                    }
                } else if RACE_TIMER_FEATURE_ENABLED && viewModel.raceTimerEnabled {
                    // Waiting for start - show stopwatch icon + time
                    VStack(spacing: 4) {
                        HStack(spacing: 4) {
                            Image(systemName: "stopwatch")
                                .font(.title)
                                .foregroundColor(.cyan)
                            Text(String(format: "%d:%02d", viewModel.raceTimerMinutes, 0))
                                .font(.system(size: 36, weight: .bold, design: .rounded))
                                .foregroundColor(.white)
                        }
                    }
                } else {
                    // Normal speed display (no race timer)
                    HStack(alignment: .lastTextBaseline, spacing: 2) {
                        Text(speedText)
                            .font(.system(size: 36, weight: .bold, design: .rounded))
                            .foregroundColor(.white)
                        Text("kts")
                            .font(.caption)
                            .foregroundColor(.gray)
                    }
                }

                // Fitness metrics row: heart rate, distance, and battery
                HStack(spacing: 12) {
                    if !viewModel.isIdleMode {
                        // Heart rate (if enabled and available) - throttled to 1Hz
                        if viewModel.heartRateEnabled && displayedHeartRate > 0 {
                            HStack(spacing: 2) {
                                Image(systemName: "heart.fill")
                                    .font(.system(size: 10))
                                    .foregroundColor(.red)
                                Text("\(displayedHeartRate)")
                                    .font(.system(size: 14, weight: .semibold))
                                    .foregroundColor(.white)
                            }
                        }

                        // Distance traveled - always show during tracking
                        HStack(spacing: 2) {
                            Image(systemName: "arrow.triangle.swap")
                                .font(.system(size: 10))
                                .foregroundColor(.cyan)
                            Text(distanceText)
                                .font(.system(size: 14, weight: .semibold))
                                .foregroundColor(.white)
                        }
                    }

                    // Battery percentage - show in both tracking and idle
                    HStack(spacing: 2) {
                        Image(systemName: batteryIconName)
                            .font(.system(size: 10))
                            .foregroundColor(batteryColor)
                        Text(batteryText)
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundColor(.white)
                    }
                }

                // ACK% and "Tap to stop" lines removed - status shown via color coding above

                // Error message
                if let error = viewModel.errorMessage {
                    Text(error)
                        .font(.system(size: 10))
                        .foregroundColor(.red)
                        .multilineTextAlignment(.center)
                        .lineLimit(2)
                }

                Spacer()
                    .frame(height: 4)

                // Assist / Cancel Assist button (only show if assist is enabled and not in idle mode)
                // Compiled out of App Store builds (sailor-side assist request removed).
                #if !APPSTORE
                if viewModel.assistEnabled && !viewModel.isIdleMode {
                    Button {
                        if viewModel.assistRequested {
                            viewModel.toggleAssist() // Cancel immediately
                        } else {
                            showAssistConfirmation = true
                        }
                    } label: {
                        Text(viewModel.assistRequested ? "CANCEL ASSIST" : "ASSIST")
                            .font(.caption)
                            .bold()
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 10)
                            .background(viewModel.assistRequested ? Color.red : Color.gray.opacity(0.4))
                            .foregroundColor(.white)
                            .cornerRadius(20)
                    }
                    .buttonStyle(.plain)
                    .padding(.horizontal, 12)
                }
                #endif
            }
            .padding(.bottom, 4)

            // "Stop for settings" brief message
            if showSettingsBlocked {
                VStack {
                    Spacer()
                    Text("Stop tracking\nto change settings")
                        .font(.caption)
                        .bold()
                        .foregroundColor(.white)
                        .multilineTextAlignment(.center)
                        .padding(12)
                        .background(Color.black.opacity(0.85))
                        .cornerRadius(12)
                    Spacer()
                }
            }

            // Slide-to-stop confirmation overlay
            if showStopConfirmation {
                SlideToConfirmOverlay(
                    title: "Slide to Stop",
                    fillColor: .red,
                    thumbLabel: "■",
                    onConfirm: {
                        showStopConfirmation = false
                        viewModel.stopTracking()
                    },
                    onDismiss: {
                        showStopConfirmation = false
                    }
                )
            }

            // Slide-to-assist confirmation overlay
            // Compiled out of App Store builds (sailor-side assist request removed).
            #if !APPSTORE
            if showAssistConfirmation {
                SlideToConfirmOverlay(
                    title: "Slide for Assist",
                    fillColor: Color(red: 1, green: 0.53, blue: 0),
                    thumbLabel: "⚠",
                    onConfirm: {
                        showAssistConfirmation = false
                        viewModel.toggleAssist()
                    },
                    onDismiss: {
                        showAssistConfirmation = false
                    }
                )
            }
            #endif
        }
        .navigationBarBackButtonHidden(true)
        .onTapGesture {
            if viewModel.isTracking {
                showStopConfirmation = true
            }
        }
        // Auto-dismiss overlays
        .task(id: showStopConfirmation) {
            if showStopConfirmation {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                showStopConfirmation = false
            }
        }
        .task(id: showAssistConfirmation) {
            if showAssistConfirmation {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                showAssistConfirmation = false
            }
        }
        .task(id: showSettingsBlocked) {
            if showSettingsBlocked {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                showSettingsBlocked = false
            }
        }
        // Dismiss overlays if tracking stops externally
        .onChange(of: viewModel.isTracking) { isTracking in
            if !isTracking {
                showStopConfirmation = false
                showAssistConfirmation = false
            }
        }
        .onChange(of: viewModel.currentHeartRate) { newValue in
            // Throttle heart rate updates to at most once per second
            let now = Date()
            if now.timeIntervalSince(lastHeartRateUpdate) >= 1.0 {
                displayedHeartRate = newValue
                lastHeartRateUpdate = now
            }
        }
        .onAppear {
            // Initialize displayed heart rate
            displayedHeartRate = viewModel.currentHeartRate
        }
    }

    // MARK: - Computed Properties

    private var speedText: String {
        guard let pos = viewModel.lastPosition else {
            return "0.0"
        }
        return String(format: "%.1f", pos.speedKnots)
    }

    private var distanceText: String {
        let meters = viewModel.totalDistance
        if meters < 1000 {
            return String(format: "%.0fm", meters)
        } else {
            return String(format: "%.1fkm", meters / 1000)
        }
    }

    private var batteryLevel: Float {
        let device = WKInterfaceDevice.current()
        device.isBatteryMonitoringEnabled = true
        return device.batteryLevel
    }

    private var batteryText: String {
        let level = batteryLevel
        if level < 0 {
            return "--%"
        }
        return "\(Int(level * 100))%"
    }

    private var batteryIconName: String {
        let level = batteryLevel
        if level < 0 {
            return "battery.0"
        } else if level < 0.15 {
            return "battery.0"
        } else if level < 0.40 {
            return "battery.25"
        } else if level < 0.65 {
            return "battery.50"
        } else if level < 0.90 {
            return "battery.75"
        } else {
            return "battery.100"
        }
    }

    private var batteryColor: Color {
        let level = batteryLevel
        if level < 0 {
            return .gray
        } else if level < 0.20 {
            return .red
        } else if level < 0.40 {
            return .yellow
        } else {
            return .green
        }
    }

    private var ackRateColor: Color {
        let rate = viewModel.ackRatePercent
        if rate >= 80 {
            return .green
        } else if rate >= 50 {
            return .yellow
        } else {
            return .red
        }
    }

    private var connectionColor: Color {
        let rate = viewModel.ackRatePercent
        if rate >= 80 {
            return .green
        } else if rate >= 50 {
            return .yellow
        } else if viewModel.packetsSent > 0 {
            return .red
        } else {
            return .gray
        }
    }

    /// Color for TRACKING status based on last ACK time
    private var trackingStatusColor: Color {
        guard let lastAck = viewModel.connectionStatus.lastAckTime else {
            return .red  // No ACK received yet
        }

        let timeSinceAck = Date().timeIntervalSince(lastAck)
        if timeSinceAck < 30 {
            return .green  // ACK within last 30s
        } else if timeSinceAck < 60 {
            return .orange  // ACK between 30-60s ago
        } else {
            return .red  // ACK more than 60s ago
        }
    }
}

// MARK: - Slide to Confirm Overlay

/// Reusable slide-to-confirm overlay matching WearOS design
private struct SlideToConfirmOverlay: View {
    let title: String
    let fillColor: Color
    let thumbLabel: String
    let onConfirm: () -> Void
    let onDismiss: () -> Void

    private let trackWidth: CGFloat = 140
    private let thumbSize: CGFloat = 40
    private let threshold: CGFloat = 0.85

    @State private var dragOffset: CGFloat = 0
    @State private var isDragging = false
    @State private var hasReachedThreshold = false

    private var maxDrag: CGFloat { trackWidth - thumbSize }
    private var progress: CGFloat { maxDrag > 0 ? min(max(dragOffset / maxDrag, 0), 1) : 0 }

    var body: some View {
        // Full-screen dark overlay - tap to dismiss
        ZStack {
            Color.black.opacity(0.85)
                .ignoresSafeArea()
                .onTapGesture { onDismiss() }

            VStack(spacing: 12) {
                Text(title)
                    .font(.system(size: 16, weight: .bold))
                    .foregroundColor(fillColor)

                // Slider track
                ZStack(alignment: .leading) {
                    // Track background
                    RoundedRectangle(cornerRadius: thumbSize / 2)
                        .fill(Color(white: 0.2))
                        .frame(width: trackWidth, height: thumbSize)

                    // Color fill following thumb
                    RoundedRectangle(cornerRadius: thumbSize / 2)
                        .fill(fillColor.opacity(progress >= threshold ? 0.8 : 0.4))
                        .frame(width: dragOffset + thumbSize, height: thumbSize)

                    // Draggable thumb
                    Circle()
                        .fill(Color.white)
                        .frame(width: thumbSize, height: thumbSize)
                        .overlay(
                            Text(thumbLabel)
                                .font(.system(size: 16))
                                .foregroundColor(fillColor)
                        )
                        .offset(x: dragOffset)
                        .gesture(
                            DragGesture()
                                .onChanged { value in
                                    isDragging = true
                                    dragOffset = min(max(value.translation.width, 0), maxDrag)
                                    let currentProgress = dragOffset / maxDrag
                                    if currentProgress >= threshold && !hasReachedThreshold {
                                        hasReachedThreshold = true
                                        WKInterfaceDevice.current().play(.click)
                                    } else if currentProgress < threshold {
                                        hasReachedThreshold = false
                                    }
                                }
                                .onEnded { _ in
                                    isDragging = false
                                    if dragOffset / maxDrag >= threshold {
                                        onConfirm()
                                    }
                                    withAnimation(.spring(response: 0.3, dampingFraction: 0.6)) {
                                        dragOffset = 0
                                    }
                                    hasReachedThreshold = false
                                }
                        )
                }
                .frame(width: trackWidth, height: thumbSize)
            }
        }
    }
}

#Preview {
    WatchTrackingView()
        .environmentObject(WatchTrackerViewModel())
}
