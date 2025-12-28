import Foundation
import Combine
import WatchKit
import HealthKit

/// View model for watchOS app
@MainActor
public class WatchTrackerViewModel: NSObject, ObservableObject {
    // MARK: - Tracking State

    @Published public var isTracking = false
    @Published public var assistRequested = false
    @Published public var lastPosition: TrackerPosition?
    @Published public var connectionStatus = ConnectionStatus()
    @Published public var eventName = ""
    @Published public var statusLine = "---"  // GPS wait, connecting..., auth failure, or event name
    @Published public var assistEnabled = true  // Whether assist button should be shown
    @Published public var errorMessage: String?

    // MARK: - Display State (for UI updates)

    @Published public var ackRatePercent: Int = 0
    @Published public var packetsSent: Int = 0
    @Published public var packetsAcked: Int = 0
    @Published public var workoutState: String = ""

    // MARK: - Workout Session (for background tracking)

    private let healthStore = HKHealthStore()
    private var workoutSession: HKWorkoutSession?
    private var workoutBuilder: HKLiveWorkoutBuilder?

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

    @Published public var heartRateEnabled: Bool {
        didSet { preferences.heartRateEnabled = heartRateEnabled }
    }

    @Published public var password: String {
        didSet { preferences.password = password }
    }

    @Published public var eventId: Int {
        didSet { preferences.eventId = eventId }
    }

    /// Returns true if required settings are missing
    public var needsSetup: Bool {
        sailorId.isEmpty || password.isEmpty
    }

    // MARK: - Event List

    @Published public var events: [EventInfo] = []
    @Published public var eventsLoading = false

    // MARK: - Private

    private let preferences = PreferencesManager.shared
    private var cancellables = Set<AnyCancellable>()

    // MARK: - Initialization

    public override init() {
        // Load from preferences (before super.init)
        let prefs = PreferencesManager.shared
        self.sailorId = prefs.sailorId
        self.serverHost = prefs.serverHost
        self.role = prefs.role
        self.highFrequencyMode = prefs.highFrequencyMode
        self.heartRateEnabled = prefs.heartRateEnabled
        self.password = prefs.password
        self.eventId = prefs.eventId

        super.init()
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
                self?.ackRatePercent = Int(status.ackRate)
                self?.packetsSent = status.packetsSent
                self?.packetsAcked = status.packetsAcked
            }
            .store(in: &cancellables)

        // Subscribe to event name
        TrackerService.shared.eventNamePublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] name in
                self?.eventName = name
            }
            .store(in: &cancellables)

        // Subscribe to status line (GPS wait, connecting, auth failure, or event name)
        TrackerService.shared.statusLinePublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] status in
                self?.statusLine = status
            }
            .store(in: &cancellables)

        // Subscribe to assist enabled status
        TrackerService.shared.assistEnabledPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] enabled in
                self?.assistEnabled = enabled
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

        // Start tracking
        errorMessage = "Starting..."

        Task {
            do {
                try await TrackerService.shared.start()
                // Clear error and haptic for success
                errorMessage = nil
                WKInterfaceDevice.current().play(.success)

                // Start heart rate monitoring if enabled
                if heartRateEnabled {
                    let authorized = await HeartRateMonitor.shared.requestAuthorization()
                    if authorized {
                        HeartRateMonitor.shared.startMonitoring()
                    }
                }

                // Try to start workout session in background for background mode support
                workoutState = "starting..."
                Task.detached { [weak self] in
                    do {
                        try await self?.startWorkoutSession()
                    } catch {
                        print("[WORKOUT] Session failed: \(error.localizedDescription)")
                        await MainActor.run {
                            self?.workoutState = "failed"
                        }
                    }
                }
            } catch {
                errorMessage = error.localizedDescription
                WKInterfaceDevice.current().play(.failure)
            }
        }
    }

    // MARK: - Workout Session (for background GPS)

    private func requestWorkoutAuthorization() async -> Bool {
        let workoutType = HKObjectType.workoutType()
        let heartRateType = HKQuantityType.quantityType(forIdentifier: .heartRate)!
        let activeEnergyType = HKQuantityType.quantityType(forIdentifier: .activeEnergyBurned)!

        do {
            try await healthStore.requestAuthorization(
                toShare: [workoutType],
                read: [heartRateType, activeEnergyType]
            )
            return true
        } catch {
            print("Workout authorization failed: \(error.localizedDescription)")
            return false
        }
    }

    private func startWorkoutSession() async throws {
        // Request workout authorization first
        let authorized = await requestWorkoutAuthorization()
        if !authorized {
            print("Workout authorization not granted")
            return
        }

        // End any existing session
        await stopWorkoutSession()

        // Create workout configuration for water sports
        let configuration = HKWorkoutConfiguration()
        configuration.activityType = .sailing  // Closest to windsurfing
        configuration.locationType = .outdoor

        // Create session
        let session = try HKWorkoutSession(healthStore: healthStore, configuration: configuration)
        let builder = session.associatedWorkoutBuilder()

        // Set data source
        builder.dataSource = HKLiveWorkoutDataSource(healthStore: healthStore, workoutConfiguration: configuration)

        // Set delegates
        session.delegate = self
        builder.delegate = self

        // Store references
        workoutSession = session
        workoutBuilder = builder

        // Start session and builder
        print("[WORKOUT] Starting workout session...")
        session.startActivity(with: Date())
        try await builder.beginCollection(at: Date())
        print("[WORKOUT] Workout session started successfully")
    }

    private func stopWorkoutSession() async {
        guard let session = workoutSession, let builder = workoutBuilder else { return }

        // End workout
        session.end()

        do {
            try await builder.endCollection(at: Date())
            try await builder.finishWorkout()
        } catch {
            // Ignore errors when ending workout
        }

        workoutSession = nil
        workoutBuilder = nil
    }

    public func stopTracking() {
        Task {
            // Stop workout session
            await stopWorkoutSession()

            // Stop heart rate monitoring
            HeartRateMonitor.shared.stopMonitoring()

            await TrackerService.shared.stop()
            assistRequested = false
            errorMessage = nil  // Clear any error
            ackRatePercent = 0
            packetsSent = 0
            packetsAcked = 0
            workoutState = ""
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

// MARK: - HKWorkoutSessionDelegate

extension WatchTrackerViewModel: HKWorkoutSessionDelegate {
    nonisolated public func workoutSession(
        _ workoutSession: HKWorkoutSession,
        didChangeTo toState: HKWorkoutSessionState,
        from fromState: HKWorkoutSessionState,
        date: Date
    ) {
        Task { @MainActor in
            // Update state display
            switch toState {
            case .notStarted: workoutState = "not started"
            case .running: workoutState = "running"
            case .ended: workoutState = "ended"
            case .paused: workoutState = "paused"
            case .prepared: workoutState = "prepared"
            case .stopped: workoutState = "stopped"
            @unknown default: workoutState = "unknown"
            }
            print("[WORKOUT] State changed: \(fromState.rawValue) -> \(toState.rawValue) (\(workoutState))")

            switch toState {
            case .ended:
                // Workout ended - stop tracking if still active
                if isTracking {
                    await TrackerService.shared.stop()
                    isTracking = false
                    errorMessage = "Workout session ended"
                }
            case .paused:
                // Resume the workout if it gets paused
                print("[WORKOUT] Resuming paused workout...")
                workoutSession.resume()
            case .running:
                // Workout is running - good for background
                break
            default:
                break
            }
        }
    }

    nonisolated public func workoutSession(
        _ workoutSession: HKWorkoutSession,
        didFailWithError error: Error
    ) {
        Task { @MainActor in
            workoutState = "error"
            print("[WORKOUT] Session error: \(error.localizedDescription)")
        }
    }
}

// MARK: - HKLiveWorkoutBuilderDelegate

extension WatchTrackerViewModel: HKLiveWorkoutBuilderDelegate {
    nonisolated public func workoutBuilder(
        _ workoutBuilder: HKLiveWorkoutBuilder,
        didCollectDataOf collectedTypes: Set<HKSampleType>
    ) {
        // We don't need to process workout data - just use for background mode
    }

    nonisolated public func workoutBuilderDidCollectEvent(_ workoutBuilder: HKLiveWorkoutBuilder) {
        // Workout event collected
    }
}
