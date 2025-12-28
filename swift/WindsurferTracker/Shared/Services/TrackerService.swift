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
    public nonisolated let statusLinePublisher = CurrentValueSubject<String, Never>("---")  // GPS wait, connecting..., auth failure, or event name
    public nonisolated let errorPublisher = PassthroughSubject<TrackerError, Never>()

    // MARK: - State

    private var isRunning = false
    private var assistRequested = false
    private var sequenceNumber = 0
    private var lastAckSeq = 0
    private var acknowledgedSeqs: Set<Int> = []

    // Status line state
    private var hasGpsFix = false
    private var hasFirstAck = false
    private var hasAuthFailure = false
    private var currentEventName = ""

    // Sliding window for ACK rate (last 20 messages)
    private var ackWindow: [Bool] = []
    private let ackWindowSize = 20
    private var recordedSeqs: Set<Int> = []

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

        // Note: ACKs are handled directly from send() return value, not via subscription.
        // The ackPublisher is for external observers only.

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
        lastAckSeq = 0
        acknowledgedSeqs.removeAll()
        ackWindow.removeAll()
        recordedSeqs.removeAll()
        positionBuffer.removeAll()
        lastSendTime = nil

        // Reset status line state
        hasGpsFix = false
        hasFirstAck = false
        hasAuthFailure = false
        currentEventName = ""
        updateStatusLine()  // Show "GPS wait"

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

        // Mark GPS as ready and update status line
        if !hasGpsFix {
            hasGpsFix = true
            updateStatusLine()  // Show "connecting ..."
        }

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

        // Send packet
        let response = await networkManager.send(packet)

        if let response = response {
            await handleACK(response)
        } else {
            // No ACK received - record failure
            recordSendResult(seq: seq, success: false)
        }
        updateConnectionStatus()
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

        // Send packet
        let response = await networkManager.send(packet)

        if let response = response {
            await handleACK(response)
        } else {
            // No ACK received - record failure
            recordSendResult(seq: seq, success: false)
        }
        updateConnectionStatus()
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

    /// Record send result in sliding window (only once per sequence number)
    private func recordSendResult(seq: Int, success: Bool) {
        // Only record once per sequence number
        guard !recordedSeqs.contains(seq) else { return }
        recordedSeqs.insert(seq)

        // Add to sliding window
        ackWindow.append(success)

        // Maintain window size
        while ackWindow.count > ackWindowSize {
            ackWindow.removeFirst()
        }

        // Clean up old sequence numbers to prevent memory growth
        if recordedSeqs.count > 100 {
            let threshold = sequenceNumber - 100
            recordedSeqs = recordedSeqs.filter { $0 > threshold }
        }
    }

    private func handleACK(_ response: AckResponse) async {
        // Handle auth error
        if response.isAuthError {
            hasAuthFailure = true
            updateStatusLine()  // Show "auth failure"
            errorPublisher.send(.authenticationFailed(response.msg))
            return  // Don't count auth failures as successful ACK
        }

        // Clear auth failure on successful ACK
        hasAuthFailure = false

        // Track acknowledged sequence
        if !acknowledgedSeqs.contains(response.ack) {
            acknowledgedSeqs.insert(response.ack)
            lastAckSeq = response.ack

            // Record success in sliding window
            recordSendResult(seq: response.ack, success: true)

            // Limit set size
            if acknowledgedSeqs.count > 100 {
                acknowledgedSeqs.removeFirst()
            }
        }

        // Handle event name and update status
        if let eventName = response.event {
            currentEventName = eventName
            hasFirstAck = true
            updateStatusLine()  // Show event name
            eventNamePublisher.send(eventName)
        } else if !hasFirstAck {
            // First ACK but no event name yet
            hasFirstAck = true
            updateStatusLine()
        }

        updateConnectionStatus()
    }

    private func updateConnectionStatus() {
        // Calculate ACK rate from sliding window
        let ackRate: Double
        if ackWindow.isEmpty {
            ackRate = 0
        } else {
            let successCount = ackWindow.filter { $0 }.count
            ackRate = Double(successCount) / Double(ackWindow.count) * 100
        }

        let status = ConnectionStatus(
            ackRate: ackRate,
            lastAckSeq: lastAckSeq,
            packetsSent: ackWindow.count,
            packetsAcked: ackWindow.filter { $0 }.count,
            usingHttpFallback: false  // Will update from network manager
        )

        connectionStatusPublisher.send(status)
    }

    /// Update the status line based on current state
    private func updateStatusLine() {
        let status: String
        if hasAuthFailure {
            status = "auth failure"
        } else if hasFirstAck && !currentEventName.isEmpty {
            status = currentEventName
        } else if hasGpsFix {
            status = "connecting ..."
        } else {
            status = "GPS wait"
        }
        statusLinePublisher.send(status)
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
