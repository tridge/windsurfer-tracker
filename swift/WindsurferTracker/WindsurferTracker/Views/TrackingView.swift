import SwiftUI

/// Active tracking status display - matches Android layout
struct TrackingView: View {
    @EnvironmentObject var viewModel: TrackerViewModel

    var body: some View {
        VStack(spacing: 0) {
            // Status section
            VStack(alignment: .leading, spacing: 8) {
                // Event name (always show, with placeholder when not yet received)
                Text(viewModel.eventName.isEmpty ? "---" : viewModel.eventName)
                    .font(.headline)
                    .fontWeight(.bold)
                    .foregroundColor(Color(red: 0, green: 0.4, blue: 0.67))

                // Frequency mode indicator (always show)
                Text(viewModel.highFrequencyMode ? "1Hz MODE" : "0.1Hz MODE")
                    .font(.caption)
                    .fontWeight(.bold)
                    .foregroundColor(Color(red: 0, green: 0.67, blue: 0.67))

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

                // Speed and Course row
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
            }
            .padding(16)

            Spacer(minLength: 16)

            // Assist button - large and prominent
            AssistButton(
                isActive: viewModel.assistRequested,
                onToggle: {
                    viewModel.toggleAssist()
                }
            )
            .frame(minHeight: 80, maxHeight: 120)
            .padding(.horizontal, 16)

            Spacer(minLength: 16)

            // Stop button
            Button {
                viewModel.showStopConfirmation = true
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

#Preview {
    TrackingView()
        .environmentObject(TrackerViewModel())
}
