import Foundation

/// Configuration constants for the Windsurfer Tracker
public enum TrackerConfig {
    // MARK: - Server Defaults
    public static let defaultServerHost = "wstracker.org"
    public static let defaultServerPort: UInt16 = 41234

    // MARK: - Timing
    public static let locationIntervalSeconds: TimeInterval = 10
    public static let udpRetryCount = 3
    public static let udpRetryDelaySeconds: TimeInterval = 1.5
    public static let ackTimeoutSeconds: TimeInterval = 2.0
    public static let dnsRefreshInterval: TimeInterval = 300  // 5 minutes
    public static let httpRetryIntervalSeconds: TimeInterval = 60  // Try UDP again after 60s

    // MARK: - Thresholds
    public static let maxAccuracyMeters: Double = 100.0
    public static let httpFallbackThreshold = 1  // Switch to HTTP after this many UDP failures (was 3, lowered for watch debugging)
    public static let highFrequencyBatchSize = 10  // Positions per packet in 1Hz mode
    public static let batteryDrainMinMinutes: TimeInterval = 5  // Min time before reporting drain rate

    // MARK: - Packet
    public static let signalStrengthUnavailable = -1  // iOS doesn't expose cell signal

    // MARK: - Default IDs
    public static let defaultSailorIdPrefix = "S"  // S for Swift/iOS
    public static let defaultWatchIdPrefix = "W"   // W for Watch
}

/// User roles for tracking
public enum TrackerRole: String, CaseIterable, Codable {
    case sailor = "sailor"
    case support = "support"
    case spectator = "spectator"

    public var displayName: String {
        switch self {
        case .sailor: return "Sailor"
        case .support: return "Support"
        case .spectator: return "Spectator"
        }
    }
}
