import Foundation
#if canImport(ActivityKit)
import ActivityKit
#endif

/// Activity attributes for the Live Activity tracker widget
/// Used by both the main app and the widget extension
#if os(iOS)
@available(iOS 16.2, *)
public struct TrackerActivityAttributes: ActivityAttributes {
    /// Static context set when activity starts
    public var sailorId: String
    public var eventId: Int

    public init(sailorId: String, eventId: Int) {
        self.sailorId = sailorId
        self.eventId = eventId
    }

    /// Dynamic state that updates throughout the activity
    public struct ContentState: Codable, Hashable {
        /// Whether connected (received ACK recently)
        public var isConnected: Bool
        /// Current speed in knots
        public var speedKnots: Double
        /// ACK rate percentage (0-100)
        public var ackRatePercent: Int
        /// Status line (event name, "connecting...", "GPS wait", "auth failure")
        public var statusLine: String
        /// Whether assist is currently active
        public var assistActive: Bool
        /// Whether tracking has been stopped (for final Live Activity display)
        public var isStopped: Bool

        public init(
            isConnected: Bool = false,
            speedKnots: Double = 0,
            ackRatePercent: Int = 0,
            statusLine: String = "---",
            assistActive: Bool = false,
            isStopped: Bool = false
        ) {
            self.isConnected = isConnected
            self.speedKnots = speedKnots
            self.ackRatePercent = ackRatePercent
            self.statusLine = statusLine
            self.assistActive = assistActive
            self.isStopped = isStopped
        }
    }
}
#endif
