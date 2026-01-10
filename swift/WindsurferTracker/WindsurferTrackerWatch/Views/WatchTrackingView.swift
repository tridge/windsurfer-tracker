import SwiftUI

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
                    Text("TRACKING")
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
                if let countdown = viewModel.countdownSeconds {
                    // Race countdown timer display
                    if countdown > 0 {
                        // Timer running - show remaining time
                        let minutes = countdown / 60
                        let seconds = countdown % 60
                        let countdownColor: Color = {
                            if countdown <= 10 { return .red }
                            if countdown <= 30 { return .yellow }
                            return .cyan
                        }()

                        VStack(spacing: 2) {
                            Text(String(format: "%d:%02d", minutes, seconds))
                                .font(.system(size: 42, weight: .bold, design: .rounded))
                                .foregroundColor(countdownColor)
                        }
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
                } else if viewModel.raceTimerEnabled {
                    // Waiting for start - show stopwatch icon + time (no START button)
                    HStack(spacing: 4) {
                        Image(systemName: "stopwatch")
                            .font(.title)
                            .foregroundColor(.cyan)
                        Text(String(format: "%d:%02d", viewModel.raceTimerMinutes, 0))
                            .font(.system(size: 36, weight: .bold, design: .rounded))
                            .foregroundColor(.white)
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

                // Fitness metrics row: heart rate (and distance only if countdown active)
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

                    // Distance traveled (show here when countdown is active, since speed is hidden)
                    if viewModel.countdownSeconds != nil || viewModel.raceTimerEnabled {
                        HStack(spacing: 2) {
                            Image(systemName: "arrow.triangle.swap")
                                .font(.system(size: 10))
                                .foregroundColor(.cyan)
                            Text(distanceText)
                                .font(.system(size: 14, weight: .semibold))
                                .foregroundColor(.white)
                        }
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
