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

                // Speed - large and prominent, kts inline to save space
                HStack(alignment: .lastTextBaseline, spacing: 2) {
                    Text(speedText)
                        .font(.system(size: 36, weight: .bold, design: .rounded))
                        .foregroundColor(.white)
                    Text("kts")
                        .font(.caption)
                        .foregroundColor(.gray)
                }

                // Status row: ACK rate, sent/acked counts, connection indicator
                HStack(spacing: 8) {
                    // ACK rate percentage
                    Text("\(viewModel.ackRatePercent)%")
                        .font(.caption2)
                        .bold()
                        .foregroundColor(ackRateColor)

                    // Sent/Acked counts
                    Text("\(viewModel.packetsAcked)/\(viewModel.packetsSent)")
                        .font(.system(size: 10))
                        .foregroundColor(.gray)

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
}

#Preview {
    WatchTrackingView()
        .environmentObject(WatchTrackerViewModel())
}
