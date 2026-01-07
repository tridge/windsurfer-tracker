import Foundation
import Combine

/// UserDefaults wrapper for tracker preferences with Combine publishers
public final class PreferencesManager: ObservableObject {
    public static let shared = PreferencesManager()

    private let defaults: UserDefaults

    // MARK: - Preference Keys

    private enum Keys {
        static let sailorId = "sailor_id"
        static let serverHost = "server_host"
        static let serverPort = "server_port"
        static let role = "role"
        static let password = "password"
        static let eventId = "event_id"
        static let highFrequencyMode = "high_frequency_mode"
        static let heartRateEnabled = "heart_rate_enabled"
        static let trackerBeep = "tracker_beep"
        static let trackingActive = "tracking_active"
        static let batteryOptAsked = "battery_opt_asked"
    }

    // MARK: - Published Properties

    @Published public var sailorId: String {
        didSet { defaults.set(sailorId, forKey: Keys.sailorId) }
    }

    @Published public var serverHost: String {
        didSet {
            // Migrate legacy server address
            let host = serverHost == "track.tridgell.net" ? TrackerConfig.defaultServerHost : serverHost
            defaults.set(host, forKey: Keys.serverHost)
        }
    }

    @Published public var serverPort: Int {
        didSet { defaults.set(serverPort, forKey: Keys.serverPort) }
    }

    @Published public var role: TrackerRole {
        didSet { defaults.set(role.rawValue, forKey: Keys.role) }
    }

    @Published public var password: String {
        didSet { defaults.set(password, forKey: Keys.password) }
    }

    @Published public var eventId: Int {
        didSet { defaults.set(eventId, forKey: Keys.eventId) }
    }

    @Published public var highFrequencyMode: Bool {
        didSet { defaults.set(highFrequencyMode, forKey: Keys.highFrequencyMode) }
    }

    @Published public var heartRateEnabled: Bool {
        didSet { defaults.set(heartRateEnabled, forKey: Keys.heartRateEnabled) }
    }

    @Published public var trackerBeep: Bool {
        didSet { defaults.set(trackerBeep, forKey: Keys.trackerBeep) }
    }

    @Published public var trackingActive: Bool {
        didSet { defaults.set(trackingActive, forKey: Keys.trackingActive) }
    }

    @Published public var batteryOptAsked: Bool {
        didSet { defaults.set(batteryOptAsked, forKey: Keys.batteryOptAsked) }
    }

    // MARK: - Initialization

    private init() {
        // Use standard UserDefaults (app groups require proper provisioning)
        self.defaults = .standard

        // Load saved values or use defaults
        self.sailorId = defaults.string(forKey: Keys.sailorId) ?? ""

        // Migrate legacy server address
        var host = defaults.string(forKey: Keys.serverHost) ?? TrackerConfig.defaultServerHost
        if host == "track.tridgell.net" {
            host = TrackerConfig.defaultServerHost
        }
        self.serverHost = host

        let port = defaults.integer(forKey: Keys.serverPort)
        self.serverPort = port > 0 ? port : Int(TrackerConfig.defaultServerPort)

        let roleString = defaults.string(forKey: Keys.role) ?? TrackerRole.sailor.rawValue
        self.role = TrackerRole(rawValue: roleString) ?? .sailor

        self.password = defaults.string(forKey: Keys.password) ?? ""

        let eid = defaults.integer(forKey: Keys.eventId)
        self.eventId = eid > 0 ? eid : 2

        self.highFrequencyMode = defaults.bool(forKey: Keys.highFrequencyMode)
        self.heartRateEnabled = defaults.bool(forKey: Keys.heartRateEnabled)  // Default false
        // trackerBeep defaults to true - need to check if key exists
        if defaults.object(forKey: Keys.trackerBeep) == nil {
            self.trackerBeep = true
        } else {
            self.trackerBeep = defaults.bool(forKey: Keys.trackerBeep)
        }
        self.trackingActive = defaults.bool(forKey: Keys.trackingActive)
        self.batteryOptAsked = defaults.bool(forKey: Keys.batteryOptAsked)
    }

    // MARK: - Convenience Methods

    /// Generate a default sailor ID if none set
    public func generateDefaultSailorId() -> String {
        let prefix = TrackerConfig.defaultSailorIdPrefix
        let number = String(format: "%02d", Int.random(in: 1...99))
        return "\(prefix)\(number)"
    }

    /// Ensure sailor ID is set, generating one if empty
    public func ensureSailorId() {
        if sailorId.isEmpty {
            sailorId = generateDefaultSailorId()
        }
    }

    /// Reset all preferences to defaults
    public func resetToDefaults() {
        sailorId = ""
        serverHost = TrackerConfig.defaultServerHost
        serverPort = Int(TrackerConfig.defaultServerPort)
        role = .sailor
        password = ""
        eventId = 2
        highFrequencyMode = false
        heartRateEnabled = false
        trackerBeep = true
        trackingActive = false
        batteryOptAsked = false
    }

    /// Get current configuration summary for display
    public var configSummary: String {
        "\(sailorId.isEmpty ? "(not set)" : sailorId) @ \(serverHost):\(serverPort)"
    }
}
