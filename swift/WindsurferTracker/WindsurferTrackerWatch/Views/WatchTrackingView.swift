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

                // Status title
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
                        .foregroundColor(.cyan)
                }

                // Event name (if available)
                if !viewModel.eventName.isEmpty {
                    Text(viewModel.eventName)
                        .font(.caption2)
                        .foregroundColor(.blue)
                        .lineLimit(1)
                }

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

                // Speed - large and prominent
                VStack(spacing: 0) {
                    Text(speedText)
                        .font(.system(size: 48, weight: .bold, design: .rounded))
                        .foregroundColor(.white)

                    Text("kts")
                        .font(.caption)
                        .foregroundColor(.gray)
                }

                // Status row: battery, signal indicator
                HStack(spacing: 8) {
                    // Battery percentage
                    Text(batteryText)
                        .font(.caption2)
                        .bold()
                        .foregroundColor(batteryColor)

                    // Signal bar indicator (like WearOS yellow bar)
                    Rectangle()
                        .fill(signalBarColor)
                        .frame(width: 16, height: 4)
                        .cornerRadius(2)

                    // Connection dot
                    Circle()
                        .fill(connectionColor)
                        .frame(width: 8, height: 8)
                }

                // Tap to stop hint
                Text("Tap to stop")
                    .font(.system(size: 10))
                    .foregroundColor(.gray)

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

                // Assist / Cancel Assist button
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

    private var batteryText: String {
        let level = BatteryMonitor.shared.level
        return level >= 0 ? "\(level)%" : "--%"
    }

    private var batteryColor: Color {
        let level = BatteryMonitor.shared.level
        if level > 50 {
            return .green
        } else if level > 20 {
            return .yellow
        } else {
            return .red
        }
    }

    private var signalBarColor: Color {
        // Yellow bar like WearOS
        switch viewModel.connectionStatus.qualityLevel {
        case .good:
            return .yellow
        case .fair:
            return .yellow.opacity(0.6)
        case .poor:
            return .gray
        }
    }

    private var connectionColor: Color {
        switch viewModel.connectionStatus.qualityLevel {
        case .good:
            return .green
        case .fair:
            return .yellow
        case .poor:
            return .red
        }
    }
}

#Preview {
    WatchTrackingView()
        .environmentObject(WatchTrackerViewModel())
}
