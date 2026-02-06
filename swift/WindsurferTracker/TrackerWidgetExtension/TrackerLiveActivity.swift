import ActivityKit
import SwiftUI
import WidgetKit

@available(iOS 16.2, *)
struct TrackerLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: TrackerActivityAttributes.self) { context in
            // Lock screen / banner view
            LockScreenView(context: context)
        } dynamicIsland: { context in
            DynamicIsland {
                // Expanded regions
                DynamicIslandExpandedRegion(.leading) {
                    ExpandedLeadingView(context: context)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    ExpandedTrailingView(context: context)
                }
                DynamicIslandExpandedRegion(.center) {
                    ExpandedCenterView(context: context)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    ExpandedBottomView(context: context)
                }
            } compactLeading: {
                // Compact leading - windsurfer icon with color
                CompactLeadingView(context: context)
            } compactTrailing: {
                // Compact trailing - speed
                CompactTrailingView(context: context)
            } minimal: {
                // Minimal view - just icon
                MinimalView(context: context)
            }
        }
    }
}

// MARK: - Windsurfer Icon Helper

@available(iOS 16.2, *)
private struct WindsurferIcon: View {
    let isConnected: Bool
    var size: CGFloat = 32

    var body: some View {
        Image(isConnected ? "windsurfer_ok" : "windsurfer_error")
            .resizable()
            .aspectRatio(contentMode: .fit)
            .frame(width: size, height: size)
    }
}

// MARK: - Lock Screen View

@available(iOS 16.2, *)
private struct LockScreenView: View {
    let context: ActivityViewContext<TrackerActivityAttributes>

    var body: some View {
        HStack(spacing: 12) {
            // Windsurfer icon with connection status
            WindsurferIcon(isConnected: context.state.isConnected, size: 32)

            VStack(alignment: .leading, spacing: 4) {
                // Sailor ID and status
                HStack {
                    Text(context.attributes.sailorId)
                        .font(.headline)
                        .fontWeight(.semibold)
                    if context.state.assistActive {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundColor(.orange)
                    }
                }

                // Status line (event name or status)
                Text(context.state.statusLine)
                    .font(.subheadline)
                    .foregroundColor(.secondary)
            }

            Spacer()

            if context.state.isStopped {
                Text("STOPPED")
                    .font(.title3)
                    .fontWeight(.bold)
                    .foregroundColor(.red)
            } else {
                VStack(alignment: .trailing, spacing: 4) {
                    // Speed
                    HStack(spacing: 2) {
                        Text(String(format: "%.1f", context.state.speedKnots))
                            .font(.title2)
                            .fontWeight(.bold)
                            .monospacedDigit()
                        Text("kn")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }

                    // Connection rate
                    HStack(spacing: 4) {
                        Image(systemName: connectionIcon)
                            .foregroundColor(connectionColor)
                        Text("\(context.state.ackRatePercent)%")
                            .font(.caption)
                            .foregroundColor(.secondary)
                            .monospacedDigit()
                    }
                }
            }
        }
        .padding()
        .activityBackgroundTint(
            context.state.isStopped ? Color.red.opacity(0.2) :
            context.state.assistActive ? Color.orange.opacity(0.2) : Color.black.opacity(0.7)
        )
    }

    private var connectionIcon: String {
        if context.state.ackRatePercent >= 80 {
            return "antenna.radiowaves.left.and.right"
        } else if context.state.ackRatePercent >= 50 {
            return "antenna.radiowaves.left.and.right"
        } else {
            return "antenna.radiowaves.left.and.right.slash"
        }
    }

    private var connectionColor: Color {
        if context.state.ackRatePercent >= 80 {
            return .green
        } else if context.state.ackRatePercent >= 50 {
            return .yellow
        } else {
            return .red
        }
    }
}

// MARK: - Dynamic Island Compact Views

@available(iOS 16.2, *)
private struct CompactLeadingView: View {
    let context: ActivityViewContext<TrackerActivityAttributes>

    var body: some View {
        WindsurferIcon(isConnected: context.state.isConnected, size: 20)
    }
}

@available(iOS 16.2, *)
private struct CompactTrailingView: View {
    let context: ActivityViewContext<TrackerActivityAttributes>

    var body: some View {
        if context.state.isStopped {
            Text("STOP")
                .fontWeight(.semibold)
                .foregroundColor(.red)
        } else {
            HStack(spacing: 2) {
                Text(String(format: "%.1f", context.state.speedKnots))
                    .fontWeight(.semibold)
                    .monospacedDigit()
                Text("kn")
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
        }
    }
}

@available(iOS 16.2, *)
private struct MinimalView: View {
    let context: ActivityViewContext<TrackerActivityAttributes>

    var body: some View {
        if context.state.isStopped {
            Image("windsurfer_error")
                .resizable()
                .aspectRatio(contentMode: .fit)
                .frame(width: 16, height: 16)
        } else {
            WindsurferIcon(isConnected: context.state.isConnected, size: 16)
        }
    }
}

// MARK: - Dynamic Island Expanded Views

@available(iOS 16.2, *)
private struct ExpandedLeadingView: View {
    let context: ActivityViewContext<TrackerActivityAttributes>

    var body: some View {
        VStack(alignment: .leading) {
            WindsurferIcon(isConnected: context.state.isConnected, size: 24)
            Text(context.attributes.sailorId)
                .font(.caption)
                .fontWeight(.medium)
        }
    }
}

@available(iOS 16.2, *)
private struct ExpandedTrailingView: View {
    let context: ActivityViewContext<TrackerActivityAttributes>

    var body: some View {
        if context.state.isStopped {
            Text("STOPPED")
                .font(.title3)
                .fontWeight(.bold)
                .foregroundColor(.red)
        } else {
            VStack(alignment: .trailing) {
                HStack(spacing: 2) {
                    Text(String(format: "%.1f", context.state.speedKnots))
                        .font(.title2)
                        .fontWeight(.bold)
                        .monospacedDigit()
                    Text("kn")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                HStack(spacing: 4) {
                    Image(systemName: connectionIcon)
                        .font(.caption)
                    Text("\(context.state.ackRatePercent)%")
                        .font(.caption)
                        .monospacedDigit()
                }
                .foregroundColor(connectionColor)
            }
        }
    }

    private var connectionIcon: String {
        context.state.ackRatePercent >= 50 ?
            "antenna.radiowaves.left.and.right" :
            "antenna.radiowaves.left.and.right.slash"
    }

    private var connectionColor: Color {
        if context.state.ackRatePercent >= 80 {
            return .green
        } else if context.state.ackRatePercent >= 50 {
            return .yellow
        } else {
            return .red
        }
    }
}

@available(iOS 16.2, *)
private struct ExpandedCenterView: View {
    let context: ActivityViewContext<TrackerActivityAttributes>

    var body: some View {
        if context.state.isStopped {
            Text("Tracking stopped")
                .font(.caption)
                .foregroundColor(.red)
        } else {
            HStack {
                Text(context.state.statusLine)
                    .font(.caption)
                    .foregroundColor(.secondary)
                if context.state.assistActive {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundColor(.orange)
                }
            }
        }
    }
}

@available(iOS 16.2, *)
private struct ExpandedBottomView: View {
    let context: ActivityViewContext<TrackerActivityAttributes>

    var body: some View {
        if context.state.isStopped {
            HStack {
                Image(systemName: "stop.circle.fill")
                    .foregroundColor(.red)
                Text("STOPPED")
                    .font(.caption)
                    .fontWeight(.bold)
                    .foregroundColor(.red)
            }
            .padding(.vertical, 4)
        } else if context.state.assistActive {
            HStack {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundColor(.orange)
                Text("ASSIST REQUESTED")
                    .font(.caption)
                    .fontWeight(.bold)
                    .foregroundColor(.orange)
            }
            .padding(.vertical, 4)
        }
    }
}

