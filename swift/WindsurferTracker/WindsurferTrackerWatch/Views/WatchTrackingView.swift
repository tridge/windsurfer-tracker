import SwiftUI

/// Compact tracking display for watch
struct WatchTrackingView: View {
    @EnvironmentObject var viewModel: WatchTrackerViewModel

    var body: some View {
        ScrollView {
            VStack(spacing: 8) {
                // Status indicator
                HStack {
                    Circle()
                        .fill(viewModel.assistRequested ? Color.red : Color.green)
                        .frame(width: 8, height: 8)

                    Text(viewModel.assistRequested ? "ASSIST" : "TRACKING")
                        .font(.caption2)
                        .fontWeight(.bold)
                        .foregroundColor(viewModel.assistRequested ? .red : .green)
                }

                // Speed - large and prominent
                VStack(spacing: 0) {
                    Text(speedText)
                        .font(.system(size: 42, weight: .bold, design: .monospaced))
                        .foregroundColor(.white)

                    Text("kts")
                        .font(.caption2)
                        .foregroundColor(.gray)
                }

                // Compact status row
                HStack(spacing: 12) {
                    // Battery
                    statusPill(
                        icon: batteryIcon,
                        value: batteryText,
                        color: batteryColor
                    )

                    // Connection
                    statusPill(
                        icon: connectionIcon,
                        value: "",
                        color: connectionColor
                    )
                }

                Spacer()
                    .frame(height: 8)

                // Assist button
                WatchAssistButton(
                    isActive: viewModel.assistRequested,
                    onToggle: {
                        viewModel.toggleAssist()
                    }
                )

                // Stop button
                Button {
                    viewModel.stopTracking()
                } label: {
                    HStack {
                        Image(systemName: "stop.fill")
                        Text("Stop")
                    }
                    .font(.caption)
                }
                .buttonStyle(.bordered)
                .tint(.gray)
            }
            .padding(.horizontal, 4)
        }
        .navigationBarBackButtonHidden(true)
    }

    // MARK: - Computed Properties

    private var speedText: String {
        guard let pos = viewModel.lastPosition else {
            return "--.-"
        }
        return String(format: "%.1f", pos.speedKnots)
    }

    private var batteryText: String {
        let level = BatteryMonitor.shared.level
        return level >= 0 ? "\(level)%" : "--"
    }

    private var batteryIcon: String {
        let level = BatteryMonitor.shared.level
        if BatteryMonitor.shared.isCharging {
            return "battery.100.bolt"
        }
        if level > 75 {
            return "battery.100"
        } else if level > 50 {
            return "battery.75"
        } else if level > 25 {
            return "battery.50"
        } else {
            return "battery.25"
        }
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

    private var connectionIcon: String {
        viewModel.connectionStatus.ackRate >= 50 ? "circle.fill" : "circle"
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

    @ViewBuilder
    private func statusPill(icon: String, value: String, color: Color) -> some View {
        HStack(spacing: 4) {
            Image(systemName: icon)
                .font(.caption2)
            if !value.isEmpty {
                Text(value)
                    .font(.caption2)
            }
        }
        .foregroundColor(color)
    }
}

#Preview {
    WatchTrackingView()
        .environmentObject(WatchTrackerViewModel())
}
