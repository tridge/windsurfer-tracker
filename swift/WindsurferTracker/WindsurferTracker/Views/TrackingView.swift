import SwiftUI

/// Active tracking status display - matches Android layout
struct TrackingView: View {
    @EnvironmentObject var viewModel: TrackerViewModel
    @ObservedObject private var batteryMonitor = BatteryMonitor.shared

    var body: some View {
        VStack(spacing: 0) {
            trackingContent
        }
        .background(Color.white)
        .confirmationDialog(
            "Stop Tracking?",
            isPresented: $viewModel.showStopConfirmation,
            titleVisibility: .visible
        ) {
            Button("Stop", role: .destructive) {
                viewModel.stopTracking()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Are you sure you want to stop tracking? Your position will no longer be reported.")
        }
    }

    // MARK: - Active Tracking Content

    private var trackingContent: some View {
        VStack(spacing: 0) {
            // Status section
            VStack(alignment: .leading, spacing: 8) {
                // Status line (GPS wait, connecting..., auth failure, or event name)
                Text(viewModel.statusLine)
                    .font(.headline)
                    .fontWeight(.bold)
                    .foregroundColor(viewModel.statusLine == "auth failure" ? .red : Color(red: 0, green: 0.4, blue: 0.67))

                // Sailor ID
                Text(viewModel.sailorId)
                    .font(.caption)
                    .foregroundColor(Color(white: 0.53))

                // Position
                VStack(alignment: .leading, spacing: 2) {
                    Text("Position")
                        .font(.caption)
                        .fontWeight(.bold)
                        .foregroundColor(Color(white: 0.27))

                    Text(positionText)
                        .font(.system(size: 18, design: .monospaced))
                        .foregroundColor(.black)
                        .fixedSize(horizontal: false, vertical: true)
                }

                // Speed, Course, and Distance row
                HStack(spacing: 0) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Speed")
                            .font(.caption)
                            .fontWeight(.bold)
                            .foregroundColor(Color(white: 0.27))

                        Text(speedText + " kn")
                            .font(.system(size: 26, weight: .regular, design: .monospaced))
                            .foregroundColor(.black)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)

                    VStack(alignment: .leading, spacing: 2) {
                        Text("Course")
                            .font(.caption)
                            .fontWeight(.bold)
                            .foregroundColor(Color(white: 0.27))

                        Text(headingText)
                            .font(.system(size: 26, weight: .regular, design: .monospaced))
                            .foregroundColor(.black)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)

                    VStack(alignment: .leading, spacing: 2) {
                        Text("Dist")
                            .font(.caption)
                            .fontWeight(.bold)
                            .foregroundColor(Color(white: 0.27))

                        Text(distanceText)
                            .font(.system(size: 26, weight: .regular, design: .monospaced))
                            .foregroundColor(.black)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.top, 12)

                // Connection, Last ACK, Updated row
                HStack(spacing: 0) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Connection")
                            .font(.caption)
                            .fontWeight(.bold)
                            .foregroundColor(Color(white: 0.27))

                        Text(ackRateText)
                            .font(.system(size: 20))
                            .foregroundColor(ackRateColor)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)

                    VStack(alignment: .leading, spacing: 2) {
                        Text("Last ACK")
                            .font(.caption)
                            .fontWeight(.bold)
                            .foregroundColor(Color(white: 0.27))

                        Text("ACK #\(viewModel.connectionStatus.lastAckSeq)")
                            .font(.system(size: 16))
                            .foregroundColor(.black)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)

                    VStack(alignment: .leading, spacing: 2) {
                        Text("Updated")
                            .font(.caption)
                            .fontWeight(.bold)
                            .foregroundColor(Color(white: 0.27))

                        Text(updatedText)
                            .font(.system(size: 16, design: .monospaced))
                            .foregroundColor(.black)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.top, 12)

                // Live Tracking link
                VStack(alignment: .leading, spacing: 4) {
                    Text("Live Tracking")
                        .font(.caption)
                        .fontWeight(.bold)
                        .foregroundColor(Color(white: 0.27))

                    if let url = URL(string: "https://\(viewModel.serverHost)/event.html?eid=\(viewModel.eventId)") {
                        Link(destination: url) {
                            Text("https://\(viewModel.serverHost)/event.html?eid=\(viewModel.eventId)")
                                .font(.caption)
                                .foregroundColor(.blue)
                        }
                    }
                }
                .padding(.top, 12)
            }
            .padding(16)

            // Warning banners for settings that cause unreliable tracking
            VStack(spacing: 8) {
                if !viewModel.hasAlwaysPermission && viewModel.locationAuthStatus == .authorizedWhenInUse {
                    WarningBanner(
                        icon: "location.slash",
                        title: "Location set to 'When In Use'",
                        subtitle: "Change to 'Always' in Settings for reliable background tracking"
                    )
                }

                if viewModel.backgroundRefreshDisabled {
                    WarningBanner(
                        icon: "arrow.clockwise.circle",
                        title: "Background App Refresh disabled",
                        subtitle: "Enable in Settings for reliable tracking"
                    )
                }

                if batteryMonitor.isLowPowerMode {
                    WarningBanner(
                        icon: "battery.25",
                        title: "Low Power Mode enabled",
                        subtitle: "May reduce GPS accuracy and background activity"
                    )
                }
            }
            .padding(.horizontal, 16)

            Spacer(minLength: 16)

            // Assist button - large and prominent (only show if assist is enabled for this event)
            if viewModel.assistEnabled {
                AssistButton(
                    isActive: viewModel.assistRequested,
                    onToggle: {
                        viewModel.toggleAssist()
                    }
                )
                .frame(minHeight: 80, maxHeight: 120)
                .padding(.horizontal, 16)
            }

            Spacer(minLength: 16)

            // Stop button
            Button {
                // Only show confirmation if no error alert is showing
                if !viewModel.showError {
                    viewModel.showStopConfirmation = true
                }
            } label: {
                Text("Stop Tracking")
                    .font(.title3)
                    .fontWeight(.bold)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(Color(white: 0.87))
                    .foregroundColor(.black)
                    .cornerRadius(4)
            }
            .padding(.horizontal, 16)
            .padding(.top, 16)

            // Settings button
            Button {
                viewModel.showSettings = true
            } label: {
                Text("Settings")
                    .font(.body)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(Color(white: 0.73))
                    .foregroundColor(.black)
                    .cornerRadius(4)
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 16)
        }
    }

    // MARK: - Computed Properties

    private var positionText: String {
        guard let pos = viewModel.lastPosition else {
            return "---.----- ----.-----"
        }
        return "\(pos.formattedLatitude) \(pos.formattedLongitude)"
    }

    private var speedText: String {
        guard let pos = viewModel.lastPosition else {
            return "--"
        }
        return String(format: "%.1f", pos.speedKnots)
    }

    private var headingText: String {
        guard let pos = viewModel.lastPosition else {
            return "---°"
        }
        return String(format: "%03d°", pos.heading)
    }

    private var distanceText: String {
        let km = viewModel.totalDistanceMeters / 1000.0
        return String(format: "%.1f km", km)
    }

    private var ackRateText: String {
        String(format: "%.0f%%", viewModel.connectionStatus.ackRate)
    }

    private var updatedText: String {
        guard viewModel.lastPosition != nil else {
            return "--:--:--"
        }
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: Date())
    }

    private var ackRateColor: Color {
        switch viewModel.connectionStatus.qualityLevel {
        case .good:
            return Color(red: 0, green: 0.53, blue: 0)  // Dark green
        case .fair:
            return Color(red: 0.8, green: 0.4, blue: 0)  // Dark orange
        case .poor:
            return Color(red: 0.8, green: 0, blue: 0)    // Dark red
        }
    }
}

// MARK: - Warning Banner

/// Tappable warning banner that opens Settings
private struct WarningBanner: View {
    let icon: String
    let title: String
    let subtitle: String

    var body: some View {
        Button {
            if let url = URL(string: UIApplication.openSettingsURLString) {
                UIApplication.shared.open(url)
            }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.title2)
                    .foregroundColor(.orange)

                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.subheadline)
                        .fontWeight(.semibold)
                        .foregroundColor(.orange)

                    Text(subtitle)
                        .font(.caption)
                        .foregroundColor(.gray)
                }
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.orange.opacity(0.1))
            .cornerRadius(8)
        }
    }
}

#Preview {
    TrackingView()
        .environmentObject(TrackerViewModel())
}
