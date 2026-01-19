import SwiftUI
import WatchKit

/// Compact tracking display for watch - matches WearOS design
struct WatchTrackingView: View {
    @EnvironmentObject var viewModel: WatchTrackerViewModel

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
                    NavigationLink {
                        WatchSettingsView()
                            .environmentObject(viewModel)
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

                // Sailor ID with 1Hz indicator
                HStack(spacing: 4) {
                    Text(viewModel.sailorId)
                        .font(.caption2)
                        .foregroundColor(.white)
                    if viewModel.highFrequencyMode {
                        Text("1Hz")
                            .font(.system(size: 10))
                            .bold()
                            .foregroundColor(.cyan)
                    }
                }

                // Show countdown when active, otherwise show speed or stopwatch
                // Race timer feature is disabled for this release
                if RACE_TIMER_FEATURE_ENABLED, let countdown = viewModel.countdownSeconds {
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
                    // Heart rate (if enabled and available)
                    if viewModel.heartRateEnabled && viewModel.currentHeartRate > 0 {
                        HStack(spacing: 2) {
                            Image(systemName: "heart.fill")
                                .font(.system(size: 10))
                                .foregroundColor(.red)
                            Text("\(viewModel.currentHeartRate)")
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

                    // Battery percentage
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

                // Assist / Cancel Assist button (only show if assist is enabled for this event)
                if viewModel.assistEnabled {
                    Button {
                        viewModel.toggleAssist()
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
            }
            .padding(.bottom, 4)
        }
        .navigationBarBackButtonHidden(true)
        .onTapGesture {
            viewModel.stopTracking()
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

#Preview {
    WatchTrackingView()
        .environmentObject(WatchTrackerViewModel())
}
