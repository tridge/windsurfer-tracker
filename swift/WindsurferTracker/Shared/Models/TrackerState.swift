import Foundation

/// Current state of the tracker
public enum TrackerState: Equatable, Sendable {
    case idle
    case starting
    case tracking
    case stopping
    case error(TrackerError)

    public var isTracking: Bool {
        switch self {
        case .tracking, .starting:
            return true
        default:
            return false
        }
    }

    public var displayText: String {
        switch self {
        case .idle:
            return "Stopped"
        case .starting:
            return "Starting..."
        case .tracking:
            return "Tracking"
        case .stopping:
            return "Stopping..."
        case .error(let error):
            return error.localizedDescription
        }
    }
}

/// Tracker errors
public enum TrackerError: Error, Equatable, Sendable {
    case locationPermissionDenied
    case locationPermissionRestricted
    case locationServicesDisabled
    case networkUnavailable
    case serverUnreachable
    case authenticationFailed(String?)
    case encodingFailed
    case unknown(String)

    public var localizedDescription: String {
        switch self {
        case .locationPermissionDenied:
            return "Location permission denied"
        case .locationPermissionRestricted:
            return "Location permission restricted"
        case .locationServicesDisabled:
            return "Location services disabled"
        case .networkUnavailable:
            return "Network unavailable"
        case .serverUnreachable:
            return "Server unreachable"
        case .authenticationFailed(let message):
            return message ?? "Authentication failed"
        case .encodingFailed:
            return "Failed to encode packet"
        case .unknown(let message):
            return message
        }
    }
}

/// Connection status for UI display
public struct ConnectionStatus: Equatable, Sendable {
    /// ACK rate as percentage (0-100)
    public let ackRate: Double

    /// Last acknowledged sequence number
    public let lastAckSeq: Int

    /// Total packets sent
    public let packetsSent: Int

    /// Total ACKs received
    public let packetsAcked: Int

    /// Whether using HTTP fallback
    public let usingHttpFallback: Bool

    /// Time when last ACK was received (nil if never received)
    public let lastAckTime: Date?

    public init(
        ackRate: Double = 0,
        lastAckSeq: Int = 0,
        packetsSent: Int = 0,
        packetsAcked: Int = 0,
        usingHttpFallback: Bool = false,
        lastAckTime: Date? = nil
    ) {
        self.ackRate = ackRate
        self.lastAckSeq = lastAckSeq
        self.packetsSent = packetsSent
        self.packetsAcked = packetsAcked
        self.usingHttpFallback = usingHttpFallback
        self.lastAckTime = lastAckTime
    }

    /// Color indicator based on ACK rate
    public var qualityLevel: ConnectionQuality {
        if ackRate >= 80 {
            return .good
        } else if ackRate >= 50 {
            return .fair
        } else {
            return .poor
        }
    }
}

/// Connection quality levels
public enum ConnectionQuality {
    case good
    case fair
    case poor
}

/// Battery status for tracking
public struct BatteryStatus: Equatable, Sendable {
    /// Battery level 0-100, or -1 if unknown
    public let level: Int

    /// Whether device is charging
    public let isCharging: Bool

    /// Whether low power mode is enabled
    public let isLowPowerMode: Bool

    /// Battery drain rate in %/hour, nil if not calculated yet
    public let drainRate: Double?

    public init(
        level: Int = -1,
        isCharging: Bool = false,
        isLowPowerMode: Bool = false,
        drainRate: Double? = nil
    ) {
        self.level = level
        self.isCharging = isCharging
        self.isLowPowerMode = isLowPowerMode
        self.drainRate = drainRate
    }
}
