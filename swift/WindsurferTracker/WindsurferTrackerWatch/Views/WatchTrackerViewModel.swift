import Foundation
import Combine
import WatchKit

/// View model for watchOS app
@MainActor
public class WatchTrackerViewModel: ObservableObject {
    // MARK: - Tracking State

    @Published public var isTracking = false
    @Published public var assistRequested = false
    @Published public var lastPosition: TrackerPosition?
    @Published public var connectionStatus = ConnectionStatus()
    @Published public var eventName = ""
    @Published public var errorMessage: String?

    // MARK: - Settings

    @Published public var sailorId: String {
        didSet { preferences.sailorId = sailorId }
    }

    @Published public var serverHost: String {
        didSet { preferences.serverHost = serverHost }
    }

    @Published public var role: TrackerRole {
        didSet { preferences.role = role }
    }

    @Published public var highFrequencyMode: Bool {
        didSet { preferences.highFrequencyMode = highFrequencyMode }
    }

    @Published public var password: String {
        didSet { preferences.password = password }
    }

    @Published public var eventId: Int {
        didSet { preferences.eventId = eventId }
    }

    // MARK: - Event List

    @Published public var events: [EventInfo] = []
    @Published public var eventsLoading = false

    // MARK: - Private

    private let preferences = PreferencesManager.shared
    private var cancellables = Set<AnyCancellable>()

    // MARK: - Initialization

    public init() {
        // Load from preferences
        self.sailorId = preferences.sailorId
        self.serverHost = preferences.serverHost
        self.role = preferences.role
        self.highFrequencyMode = preferences.highFrequencyMode
        self.password = preferences.password
        self.eventId = preferences.eventId

        setupBindings()
    }

    private func setupBindings() {
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
            }
            .store(in: &cancellables)
    }

    // MARK: - Actions

    public func startTracking() {
        // Validate required fields
        if sailorId.isEmpty {
            errorMessage = "Name is required"
            WKInterfaceDevice.current().play(.failure)
            return
        }
        if password.isEmpty {
            errorMessage = "Password is required"
            WKInterfaceDevice.current().play(.failure)
            return
        }

        // Check authorization first
        let locationManager = LocationManager.shared
        if !locationManager.hasTrackingAuthorization {
            // Request authorization if not determined
            let status = locationManager.authorizationStatus
            if status == .denied || status == .restricted {
                errorMessage = "Location permission denied. Enable in Settings."
                WKInterfaceDevice.current().play(.failure)
            } else {
                locationManager.requestAuthorization()
                // Show message to user
                errorMessage = "Requesting location permission..."
            }
            return
        }

        Task {
            do {
                try await TrackerService.shared.start()
                // Clear error and haptic for success
                errorMessage = nil
                WKInterfaceDevice.current().play(.success)
            } catch {
                errorMessage = error.localizedDescription
                WKInterfaceDevice.current().play(.failure)
            }
        }
    }

    public func stopTracking() {
        Task {
            await TrackerService.shared.stop()
            assistRequested = false
            errorMessage = nil  // Clear any error
            WKInterfaceDevice.current().play(.stop)
        }
    }

    public func toggleAssist() {
        Task {
            await TrackerService.shared.toggleAssist()
            assistRequested = await TrackerService.shared.isAssistRequested
        }
    }

    public func fetchEvents() {
        eventsLoading = true
        Task {
            let networkManager = NetworkManager()
            await networkManager.configure(
                host: serverHost,
                port: UInt16(TrackerConfig.defaultServerPort)
            )
            let fetchedEvents = await networkManager.fetchEvents()
            await MainActor.run {
                self.events = fetchedEvents
                self.eventsLoading = false
                // Ensure selected event exists in list
                if !fetchedEvents.isEmpty && !fetchedEvents.contains(where: { $0.eid == self.eventId }) {
                    self.eventId = fetchedEvents.first?.eid ?? 2
                }
            }
        }
    }

    public func cycleEvent() {
        guard !events.isEmpty else { return }
        if let currentIndex = events.firstIndex(where: { $0.eid == eventId }) {
            let nextIndex = (currentIndex + 1) % events.count
            eventId = events[nextIndex].eid
        } else {
            eventId = events.first?.eid ?? 2
        }
    }

    public var currentEventName: String {
        events.first(where: { $0.eid == eventId })?.name ?? "Event \(eventId)"
    }
}
