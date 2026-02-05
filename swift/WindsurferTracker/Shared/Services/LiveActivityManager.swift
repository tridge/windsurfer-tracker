import Foundation
#if canImport(ActivityKit)
import ActivityKit
#endif

/// Manages Live Activities for showing tracking status on lock screen and Dynamic Island
/// Only available on iOS 16.1+
#if os(iOS)
@available(iOS 16.2, *)
public class LiveActivityManager {
    public static let shared = LiveActivityManager()

    private var currentActivity: Activity<TrackerActivityAttributes>?
    private var lastUpdateTime: Date?
    private let minimumUpdateInterval: TimeInterval = 1.0  // Throttle to 1 update/sec

    private init() {}

    // MARK: - Public Methods

    /// Start a new Live Activity for tracking
    /// - Parameters:
    ///   - sailorId: The sailor's ID
    ///   - eventId: The event ID
    /// - Returns: True if activity was started successfully
    @discardableResult
    public func startActivity(sailorId: String, eventId: Int) -> Bool {
        // End any existing activity first
        endActivity()

        // Check if Live Activities are supported and enabled
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            print("[LiveActivity] Activities not enabled by user")
            return false
        }

        let attributes = TrackerActivityAttributes(
            sailorId: sailorId,
            eventId: eventId
        )

        let initialState = TrackerActivityAttributes.ContentState(
            isConnected: false,
            speedKnots: 0,
            ackRatePercent: 0,
            statusLine: "GPS wait",
            assistActive: false
        )

        do {
            let activity = try Activity.request(
                attributes: attributes,
                content: .init(state: initialState, staleDate: nil),
                pushType: nil
            )
            currentActivity = activity
            lastUpdateTime = Date()
            print("[LiveActivity] Started activity: \(activity.id)")
            return true
        } catch {
            print("[LiveActivity] Failed to start: \(error.localizedDescription)")
            return false
        }
    }

    /// Update the Live Activity with new state
    /// - Parameters:
    ///   - isConnected: Whether we have recent ACKs
    ///   - speedKnots: Current speed in knots
    ///   - ackRatePercent: ACK success rate (0-100)
    ///   - statusLine: Status text to display
    ///   - assistActive: Whether assist is requested
    public func updateActivity(
        isConnected: Bool,
        speedKnots: Double,
        ackRatePercent: Int,
        statusLine: String,
        assistActive: Bool
    ) {
        guard let activity = currentActivity else { return }

        // Throttle updates to minimize battery impact
        if let lastUpdate = lastUpdateTime,
           Date().timeIntervalSince(lastUpdate) < minimumUpdateInterval {
            return
        }
        lastUpdateTime = Date()

        let newState = TrackerActivityAttributes.ContentState(
            isConnected: isConnected,
            speedKnots: speedKnots,
            ackRatePercent: ackRatePercent,
            statusLine: statusLine,
            assistActive: assistActive
        )

        Task {
            await activity.update(
                ActivityContent(state: newState, staleDate: Date().addingTimeInterval(60))
            )
        }
    }

    /// End the current Live Activity
    public func endActivity() {
        guard let activity = currentActivity else { return }

        Task {
            let finalState = TrackerActivityAttributes.ContentState(
                isConnected: false,
                speedKnots: 0,
                ackRatePercent: 0,
                statusLine: "Stopped",
                assistActive: false
            )

            await activity.end(
                ActivityContent(state: finalState, staleDate: nil),
                dismissalPolicy: .immediate
            )
            print("[LiveActivity] Ended activity: \(activity.id)")
        }

        currentActivity = nil
        lastUpdateTime = nil
    }

    /// Check if there's an active Live Activity
    public var isActivityActive: Bool {
        currentActivity != nil
    }
}

// MARK: - iOS 15 Fallback

/// Stub for iOS 15 compatibility - does nothing
public class LiveActivityManagerStub {
    public static let shared = LiveActivityManagerStub()

    private init() {}

    @discardableResult
    public func startActivity(sailorId: String, eventId: Int) -> Bool {
        return false
    }

    public func updateActivity(
        isConnected: Bool,
        speedKnots: Double,
        ackRatePercent: Int,
        statusLine: String,
        assistActive: Bool
    ) {
        // No-op on iOS 15
    }

    public func endActivity() {
        // No-op on iOS 15
    }

    public var isActivityActive: Bool {
        return false
    }
}
#endif
