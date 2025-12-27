import Foundation
import Combine
import CoreLocation

/// View model bridging TrackerService to SwiftUI
@MainActor
public class TrackerViewModel: ObservableObject {
    // MARK: - Tracking State

    @Published public var isTracking = false
    @Published public var assistRequested = false
    @Published public var lastPosition: TrackerPosition?
    @Published public var connectionStatus = ConnectionStatus()
    @Published public var eventName = ""
    @Published public var errorMessage: String?
    @Published public var showError = false

    // MARK: - Settings (bound to PreferencesManager)

    @Published public var sailorId: String
    @Published public var serverHost: String
    @Published public var serverPort: Int
    @Published public var role: TrackerRole
    @Published public var password: String
    @Published public var eventId: Int
    @Published public var highFrequencyMode: Bool

    // MARK: - UI State

    @Published public var showSettings = false
    @Published public var showStopConfirmation = false
    @Published public var events: [EventInfo] = []
    @Published public var isLoadingEvents = false

    // MARK: - Authorization

    @Published public var locationAuthStatus: CLAuthorizationStatus = .notDetermined

    // MARK: - Private

    private let preferences = PreferencesManager.shared
    private let locationManager = LocationManager.shared
    private var cancellables = Set<AnyCancellable>()

    // MARK: - Initialization

    public init() {
        // Load initial values from preferences
        self.sailorId = preferences.sailorId
        self.serverHost = preferences.serverHost
        self.serverPort = preferences.serverPort
        self.role = preferences.role
        self.password = preferences.password
        self.eventId = preferences.eventId
        self.highFrequencyMode = preferences.highFrequencyMode

        setupBindings()

        // Auto-show settings if ID or password is missing
        if sailorId.isEmpty || password.isEmpty {
            showSettings = true
        }
    }

    private func setupBindings() {
        // Sync settings changes to preferences
        $sailorId
            .dropFirst()
            .sink { [weak self] value in
                self?.preferences.sailorId = value
            }
            .store(in: &cancellables)

        $serverHost
            .dropFirst()
            .debounce(for: .milliseconds(500), scheduler: RunLoop.main)
            .sink { [weak self] value in
                self?.preferences.serverHost = value
                Task { await self?.fetchEvents() }
            }
            .store(in: &cancellables)

        $serverPort
            .dropFirst()
            .sink { [weak self] value in
                self?.preferences.serverPort = value
            }
            .store(in: &cancellables)

        $role
            .dropFirst()
            .sink { [weak self] value in
                self?.preferences.role = value
            }
            .store(in: &cancellables)

        $password
            .dropFirst()
            .sink { [weak self] value in
                self?.preferences.password = value
            }
            .store(in: &cancellables)

        $eventId
            .dropFirst()
            .sink { [weak self] value in
                self?.preferences.eventId = value
            }
            .store(in: &cancellables)

        $highFrequencyMode
            .dropFirst()
            .sink { [weak self] value in
                self?.preferences.highFrequencyMode = value
            }
            .store(in: &cancellables)

        // Subscribe to tracker state
        TrackerService.shared.statePublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] state in
                self?.isTracking = state.isTracking
            }
            .store(in: &cancellables)

        // Subscribe to position updates
        TrackerService.shared.positionPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] position in
                self?.lastPosition = position
            }
            .store(in: &cancellables)

        // Subscribe to connection status
        TrackerService.shared.connectionStatusPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] status in
                self?.connectionStatus = status
            }
            .store(in: &cancellables)

        // Subscribe to event name
        TrackerService.shared.eventNamePublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] name in
                self?.eventName = name
            }
            .store(in: &cancellables)

        // Subscribe to errors
        TrackerService.shared.errorPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] error in
                self?.errorMessage = error.localizedDescription
                self?.showError = true
            }
            .store(in: &cancellables)

        // Subscribe to location authorization
        locationManager.authorizationPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] status in
                self?.locationAuthStatus = status
            }
            .store(in: &cancellables)

        locationAuthStatus = locationManager.authorizationStatus
    }

    // MARK: - Actions

    public func startTracking() {
        // Check authorization first
        if !locationManager.hasTrackingAuthorization {
            // If denied, show error; otherwise request permission
            if locationAuthStatus == .denied || locationAuthStatus == .restricted {
                errorMessage = "Location permission denied. Please enable in Settings > Privacy > Location Services."
                showError = true
            } else {
                locationManager.requestAuthorization()
            }
            return
        }

        // Clear event name - will be set when first ACK received
        eventName = ""

        Task {
            do {
                try await TrackerService.shared.start()
            } catch let error as TrackerError {
                errorMessage = error.localizedDescription
                showError = true
            } catch {
                errorMessage = error.localizedDescription
                showError = true
            }
        }
    }

    public func stopTracking() {
        // Clear event name on stop
        eventName = ""

        Task {
            await TrackerService.shared.stop()
            assistRequested = false
        }
    }

    public func toggleAssist() {
        Task {
            await TrackerService.shared.toggleAssist()
            assistRequested = await TrackerService.shared.isAssistRequested
        }
    }

    public func requestLocationPermission() {
        locationManager.requestAuthorization()
    }

    public func fetchEvents() async {
        isLoadingEvents = true
        let networkManager = NetworkManager()
        await networkManager.configure(
            host: serverHost,
            port: UInt16(serverPort)
        )
        events = await networkManager.fetchEvents()
        isLoadingEvents = false
    }

    // MARK: - Helpers

    public var needsLocationPermission: Bool {
        !locationManager.hasTrackingAuthorization
    }

    public var hasAlwaysPermission: Bool {
        locationManager.hasAlwaysAuthorization
    }

    public var configSummary: String {
        let id = sailorId.isEmpty ? "(not set)" : sailorId
        return "\(id) @ \(serverHost):\(serverPort)"
    }
}
