import Foundation
import Combine

/// UserDefaults wrapper for tracker preferences with Combine publishers
public final class PreferencesManager: ObservableObject {
    public static let shared = PreferencesManager()

    private let defaults: UserDefaults
    private var cancellables = Set<AnyCancellable>()

    // MARK: - Preference Keys

    private enum Keys {
        static let sailorId = "sailor_id"
        static let serverHost = "server_host"
        static let serverPort = "server_port"
        static let role = "role"
        static let password = "password"
        static let eventId = "event_id"
        static let heartRateEnabled = "heart_rate_enabled"
        static let trackerBeep = "tracker_beep"
        static let waterLock = "water_lock"
        static let trackingActive = "tracking_active"
        static let batteryOptAsked = "battery_opt_asked"
        static let raceTimerEnabled = "race_timer_enabled"
        static let raceTimerMinutes = "race_timer_minutes"
        static let raceTimerTapGForce = "race_timer_tap_g_force"
        static let volumeAssist = "volume_assist"
        static let eulaAccepted = "eula_accepted"
    }

    // MARK: - Published Properties
    // Note: persistence is done via Combine subscriptions in init(), NOT didSet.
    // Using didSet with @Published is unreliable — SwiftUI Bindings from
    // @ObservedObject can bypass didSet and set the wrapper's storage directly,
    // causing writes to UserDefaults to silently never happen.

    @Published public var sailorId: String
    @Published public var serverHost: String
    @Published public var serverPort: Int
    @Published public var role: TrackerRole
    @Published public var password: String
    @Published public var eventId: Int
    @Published public var heartRateEnabled: Bool
    @Published public var trackerBeep: Bool
    @Published public var waterLock: Bool
    @Published public var trackingActive: Bool
    @Published public var batteryOptAsked: Bool
    @Published public var raceTimerEnabled: Bool
    @Published public var raceTimerMinutes: Int
    @Published public var raceTimerTapGForce: Int
    @Published public var volumeAssist: Bool
    @Published public var eulaAccepted: Bool

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

        self.heartRateEnabled = defaults.bool(forKey: Keys.heartRateEnabled)  // Default false
        // trackerBeep defaults to true - need to check if key exists
        if defaults.object(forKey: Keys.trackerBeep) == nil {
            self.trackerBeep = true
        } else {
            self.trackerBeep = defaults.bool(forKey: Keys.trackerBeep)
        }
        // waterLock defaults to false
        self.waterLock = defaults.bool(forKey: Keys.waterLock)
        self.trackingActive = defaults.bool(forKey: Keys.trackingActive)
        self.batteryOptAsked = defaults.bool(forKey: Keys.batteryOptAsked)
        self.raceTimerEnabled = defaults.bool(forKey: Keys.raceTimerEnabled)  // Default false
        let minutes = defaults.integer(forKey: Keys.raceTimerMinutes)
        self.raceTimerMinutes = minutes > 0 ? min(minutes, 9) : 5  // Default 5, range 1-9
        let gForce = defaults.integer(forKey: Keys.raceTimerTapGForce)
        self.raceTimerTapGForce = gForce > 0 ? min(max(gForce, 2), 9) : 3  // Default 3g, range 2-9g
        self.volumeAssist = defaults.bool(forKey: Keys.volumeAssist)  // Default false
        self.eulaAccepted = defaults.bool(forKey: Keys.eulaAccepted)  // Default false

        // Persist all changes via Combine subscriptions (reliable with SwiftUI Bindings)
        setupPersistence()
    }

    private func setupPersistence() {
        $sailorId.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.sailorId) }.store(in: &cancellables)
        $serverHost.dropFirst().sink { [weak self] v in
            let host = v == "track.tridgell.net" ? TrackerConfig.defaultServerHost : v
            self?.defaults.set(host, forKey: Keys.serverHost)
        }.store(in: &cancellables)
        $serverPort.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.serverPort) }.store(in: &cancellables)
        $role.dropFirst().sink { [weak self] v in self?.defaults.set(v.rawValue, forKey: Keys.role) }.store(in: &cancellables)
        $password.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.password) }.store(in: &cancellables)
        $eventId.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.eventId) }.store(in: &cancellables)
        $heartRateEnabled.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.heartRateEnabled) }.store(in: &cancellables)
        $trackerBeep.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.trackerBeep) }.store(in: &cancellables)
        $waterLock.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.waterLock) }.store(in: &cancellables)
        $trackingActive.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.trackingActive) }.store(in: &cancellables)
        $batteryOptAsked.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.batteryOptAsked) }.store(in: &cancellables)
        $raceTimerEnabled.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.raceTimerEnabled) }.store(in: &cancellables)
        $raceTimerMinutes.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.raceTimerMinutes) }.store(in: &cancellables)
        $raceTimerTapGForce.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.raceTimerTapGForce) }.store(in: &cancellables)
        $volumeAssist.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.volumeAssist) }.store(in: &cancellables)
        $eulaAccepted.dropFirst().sink { [weak self] v in self?.defaults.set(v, forKey: Keys.eulaAccepted) }.store(in: &cancellables)
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
        heartRateEnabled = false
        trackerBeep = true
        waterLock = false
        trackingActive = false
        batteryOptAsked = false
        raceTimerEnabled = false
        raceTimerMinutes = 5
        raceTimerTapGForce = 3
    }

    /// Get current configuration summary for display
    public var configSummary: String {
        "\(sailorId.isEmpty ? "(not set)" : sailorId) @ \(serverHost):\(serverPort)"
    }

    // MARK: - Event Password Cache

    /// Save password for a specific event ID for quick event switching
    public func saveEventPassword(eventId: Int, password: String) {
        defaults.set(password, forKey: "event_password_\(eventId)")
    }

    /// Get saved password for a specific event ID, or nil if none saved
    public func getEventPassword(eventId: Int) -> String? {
        defaults.string(forKey: "event_password_\(eventId)")
    }
}
