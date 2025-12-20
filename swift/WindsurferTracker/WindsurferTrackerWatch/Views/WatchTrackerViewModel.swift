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

        // Generate default ID with watch prefix if empty
        if sailorId.isEmpty {
            sailorId = "\(TrackerConfig.defaultWatchIdPrefix)\(String(format: "%02d", Int.random(in: 1...99)))"
        }

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
        Task {
            do {
                try await TrackerService.shared.start()
                // Haptic for success
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
            WKInterfaceDevice.current().play(.stop)
        }
    }

    public func toggleAssist() {
        Task {
            await TrackerService.shared.toggleAssist()
            assistRequested = await TrackerService.shared.isAssistRequested
        }
    }
}
