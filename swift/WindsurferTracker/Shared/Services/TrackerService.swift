import Foundation
import Combine
import CoreLocation
#if os(iOS)
import UIKit
#endif

/// Main tracker service coordinating location, networking, and state
public actor TrackerService {
    // MARK: - Singleton

    public static let shared = TrackerService()

    // MARK: - Publishers (nonisolated for Combine)

    public nonisolated let statePublisher = CurrentValueSubject<TrackerState, Never>(.idle)
    public nonisolated let positionPublisher = PassthroughSubject<TrackerPosition, Never>()
    public nonisolated let connectionStatusPublisher = CurrentValueSubject<ConnectionStatus, Never>(ConnectionStatus())
    public nonisolated let eventNamePublisher = CurrentValueSubject<String, Never>("")
    public nonisolated let errorPublisher = PassthroughSubject<TrackerError, Never>()

    // MARK: - State

    private var isRunning = false
    private var assistRequested = false
    private var sequenceNumber = 0
    private var packetsSent = 0
    private var packetsAcked = 0
    private var lastAckSeq = 0
    private var acknowledgedSeqs: Set<Int> = []

    // 1Hz mode buffer
    private var positionBuffer: [TrackerPosition] = []
    private var lastSendTime: Date?

    // MARK: - Dependencies

    private let locationManager = LocationManager.shared
    private let networkManager = NetworkManager()
    private let batteryMonitor = BatteryMonitor.shared
    private let preferences = PreferencesManager.shared

    private var cancellables = Set<AnyCancellable>()

    // MARK: - Initialization

    private init() {
        Task {
            await setupSubscriptions()
        }
    }

    private func setupSubscriptions() {
        // Subscribe to location updates
        locationManager.positionPublisher
            .sink { [weak self] position in
                guard let self = self else { return }
                Task {
                    await self.handlePosition(position)
                }
            }
            .store(in: &cancellables)

        // Subscribe to network ACKs
        networkManager.ackPublisher
            .sink { [weak self] response in
                guard let self = self else { return }
                Task {
                    await self.handleACK(response)
                }
            }
            .store(in: &cancellables)

        // Subscribe to location errors
        locationManager.errorPublisher
            .sink { [weak self] error in
                guard let self = self else { return }
                self.errorPublisher.send(.unknown(error.localizedDescription))
            }
            .store(in: &cancellables)
    }

    // MARK: - Public Methods

    /// Start tracking
    public func start() async throws {
        guard !isRunning else { return }

        // Check authorization
        guard locationManager.hasTrackingAuthorization else {
            throw TrackerError.locationPermissionDenied
        }

        // Configure network
        await networkManager.configure(
            host: preferences.serverHost,
            port: UInt16(preferences.serverPort)
        )

        // Reset state
        isRunning = true
        sequenceNumber = 0
        packetsSent = 0
        packetsAcked = 0
        lastAckSeq = 0
        acknowledgedSeqs.removeAll()
        positionBuffer.removeAll()
        lastSendTime = nil

        // Start battery tracking
        batteryMonitor.startDrainTracking()

        // Start location updates
        locationManager.startUpdating(highFrequency: preferences.highFrequencyMode)

        // Update state
        statePublisher.send(.tracking)

        // Save tracking state for auto-resume
        preferences.trackingActive = true
    }

    /// Stop tracking
    public func stop() async {
        guard isRunning else { return }

        isRunning = false
        assistRequested = false

        // Stop location updates
        locationManager.stopUpdating()

        // Stop battery tracking
        batteryMonitor.stopDrainTracking()

        // Clear buffer
        positionBuffer.removeAll()

        // Update state
        statePublisher.send(.idle)

        // Save tracking state
        preferences.trackingActive = false
    }

    /// Toggle assist request
    public func toggleAssist() async {
        assistRequested.toggle()

        // Send immediate position update when assist is requested
        if assistRequested, let position = locationManager.lastPosition {
            await sendPosition(position, forceImmediate: true)
        }
    }

    /// Set assist state
    public func setAssist(_ enabled: Bool) async {
        let wasEnabled = assistRequested
        assistRequested = enabled

        // Send immediate position update when assist is newly requested
        if enabled && !wasEnabled, let position = locationManager.lastPosition {
            await sendPosition(position, forceImmediate: true)
        }
    }

    /// Get current assist state
    public var isAssistRequested: Bool {
        assistRequested
    }

    /// Check if currently tracking
    public var isTracking: Bool {
        isRunning
    }

    // MARK: - Position Handling

    private func handlePosition(_ position: TrackerPosition) async {
        guard isRunning else { return }

        // Publish position for UI
        positionPublisher.send(position)

        if preferences.highFrequencyMode {
            // 1Hz mode - buffer positions
            positionBuffer.append(position)

            if positionBuffer.count >= TrackerConfig.highFrequencyBatchSize {
                await sendPositionBatch()
            }
        } else {
            // Standard mode - throttle to 10 seconds
            await sendPosition(position, forceImmediate: false)
        }
    }

    private func sendPosition(_ position: TrackerPosition, forceImmediate: Bool) async {
        // Throttle unless forced
        if !forceImmediate {
            if let lastSend = lastSendTime,
               Date().timeIntervalSince(lastSend) < TrackerConfig.locationIntervalSeconds {
                return
            }
        }

        lastSendTime = Date()
        sequenceNumber += 1
        let seq = sequenceNumber

        let packet = buildPacket(
            sequence: seq,
            position: position,
            positionArray: nil
        )

        packetsSent += 1
        updateConnectionStatus()

        // Send packet
        let response = await networkManager.send(packet)

        if let response = response {
            await handleACK(response)
        }
    }

    private func sendPositionBatch() async {
        guard !positionBuffer.isEmpty else { return }

        lastSendTime = Date()
        sequenceNumber += 1
        let seq = sequenceNumber

        // Get latest position for speed/heading
        let latestPosition = positionBuffer.last!

        // Build position array
        let posArray = positionBuffer.map { $0.toPositionArray() }

        let packet = buildPacket(
            sequence: seq,
            position: latestPosition,
            positionArray: posArray
        )

        // Clear buffer
        positionBuffer.removeAll()

        packetsSent += 1
        updateConnectionStatus()

        // Send packet
        let response = await networkManager.send(packet)

        if let response = response {
            await handleACK(response)
        }
    }

    private func buildPacket(sequence: Int, position: TrackerPosition, positionArray: [[Double]]?) -> TrackerPacket {
        let battery = batteryMonitor.status
        preferences.ensureSailorId()

        // Get heart rate if enabled
        let heartRate: Int? = preferences.heartRateEnabled ? HeartRateMonitor.shared.currentHeartRate : nil

        return TrackerPacket(
            id: preferences.sailorId,
            eid: preferences.eventId,
            sq: sequence,
            ts: position.unixTimestamp,
            lat: positionArray == nil ? position.latitude : nil,
            lon: positionArray == nil ? position.longitude : nil,
            spd: (position.speedKnots * 100).rounded() / 100,
            hdg: position.heading,
            ast: assistRequested,
            bat: battery.level,
            sig: TrackerConfig.signalStrengthUnavailable,
            role: preferences.role.rawValue,
            ver: appVersion,
            os: osVersion,
            pwd: preferences.password.isEmpty ? nil : preferences.password,
            bdr: battery.drainRate,
            chg: battery.isCharging,
            ps: battery.isLowPowerMode,
            pos: positionArray,
            hac: position.accuracy > 0 ? (position.accuracy * 100).rounded() / 100 : nil,
            hr: heartRate
        )
    }

    // MARK: - ACK Handling

    private func handleACK(_ response: AckResponse) async {
        // Track acknowledged sequence
        if !acknowledgedSeqs.contains(response.ack) {
            acknowledgedSeqs.insert(response.ack)
            packetsAcked += 1
            lastAckSeq = response.ack

            // Limit set size
            if acknowledgedSeqs.count > 100 {
                acknowledgedSeqs.removeFirst()
            }
        }

        // Handle event name
        if let eventName = response.event {
            eventNamePublisher.send(eventName)
        }

        // Handle auth error
        if response.isAuthError {
            errorPublisher.send(.authenticationFailed(response.msg))
        }

        updateConnectionStatus()
    }

    private func updateConnectionStatus() {
        let ackRate = packetsSent > 0 ? Double(packetsAcked) / Double(packetsSent) * 100 : 0

        let status = ConnectionStatus(
            ackRate: ackRate,
            lastAckSeq: lastAckSeq,
            packetsSent: packetsSent,
            packetsAcked: packetsAcked,
            usingHttpFallback: false  // Will update from network manager
        )

        connectionStatusPublisher.send(status)
    }

    // MARK: - Version Info

    private var appVersion: String {
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"

        // Try to get git hash from environment or build config
        let gitHash = Bundle.main.infoDictionary?["GIT_HASH"] as? String

        if let hash = gitHash, !hash.isEmpty {
            return "\(version)+\(build)(\(hash))"
        } else {
            return "\(version)+\(build)(swift)"
        }
    }

    private var osVersion: String {
        #if os(iOS)
        return "iOS \(UIDevice.current.systemVersion)"
        #elseif os(watchOS)
        return "watchOS \(WKInterfaceDevice.current().systemVersion)"
        #else
        return "Unknown"
        #endif
    }
}

#if os(watchOS)
import WatchKit
#endif
