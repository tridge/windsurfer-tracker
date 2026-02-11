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
    public nonisolated let assistEnabledPublisher = CurrentValueSubject<Bool, Never>(true)  // Whether assist button should be shown
    public nonisolated let remoteStopPublisher = PassthroughSubject<Void, Never>()  // Signals remote stop command from server
    public nonisolated let remoteCancelAssistPublisher = PassthroughSubject<Void, Never>()  // Signals remote cancel assist command from server
    public nonisolated let remoteStartPublisher = PassthroughSubject<Void, Never>()  // Signals remote start command (resume from idle)
    public nonisolated let remoteShutdownPublisher = PassthroughSubject<Void, Never>()  // Signals remote shutdown command (exit idle mode)

    // MARK: - State

    private var isRunning = false
    private var assistRequested = false
    private var sequenceNumber = 0
    private var lastAckSeq = 0
    private var lastAckTime: Date?  // For tracker beep feature
    private var acknowledgedSeqs: Set<Int> = []

    // Idle mode state
    private var isInIdleMode = false
    private var idleIntervalSeconds: Int = 0  // From server ACK, 0 = disabled
    private var idleTask: Task<Void, Never>?

    // Keepalive task (sends GPS-wait packets when no position was sent recently)
    private var keepaliveTask: Task<Void, Never>?

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

        // Subscribe to proactive commands (unsolicited UDP packets from server).
        // Normal ACKs are handled from send() return value; this catches only proactive pushes.
        networkManager.ackPublisher
            .filter { $0.proactive == true }
            .sink { [weak self] response in
                guard let self = self else { return }
                Task { await self.handleACK(response) }
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

        // Clean up idle mode if starting from idle
        if isInIdleMode {
            print("[TrackerService] Starting tracking from idle mode")
            idleTask?.cancel()
            idleTask = nil
            isInIdleMode = false
            locationManager.stopIdleLocationMonitoring()
        }

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
        lastAckTime = nil
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

        // Start battery tracking on main thread (Published properties)
        await MainActor.run {
            batteryMonitor.startDrainTracking()
        }

        // Start location updates
        locationManager.startUpdating()

        // Start keepalive loop (sends GPS-wait packet whenever no position was sent recently)
        lastSendTime = nil
        keepaliveTask = Task { [weak self] in
            while !Task.isCancelled {
                if let self = self {
                    let elapsed = await self.timeSinceLastSend()
                    if elapsed >= TrackerConfig.locationIntervalSeconds {
                        await self.sendGpsWaitPacket()
                    }
                }
                try? await Task.sleep(nanoseconds: UInt64(TrackerConfig.locationIntervalSeconds) * 1_000_000_000)
            }
        }

        // Update state
        statePublisher.send(.tracking)

        // Save tracking state for auto-resume on main thread (Published property)
        await MainActor.run {
            preferences.trackingActive = true
        }
    }

    /// Start in idle mode (for auto-start on app launch)
    /// Sends idle heartbeats so the server knows the device is available.
    /// If the server responds with idle=0, exits idle and shuts down.
    public func startInIdleMode() async {
        guard !isRunning && !isInIdleMode else { return }

        // Configure network
        await networkManager.configure(
            host: preferences.serverHost,
            port: UInt16(preferences.serverPort)
        )

        // Use cached idle interval, default to 15s if none known
        if idleIntervalSeconds <= 0 {
            idleIntervalSeconds = 15
        }

        // Start battery monitoring
        await MainActor.run {
            batteryMonitor.startDrainTracking()
        }

        // Start significant location monitoring to keep app alive in background
        locationManager.startIdleLocationMonitoring()

        // Enter idle mode
        await enterIdleMode()
    }

    /// Stop tracking
    public func stop() async {
        guard isRunning || isInIdleMode else { return }

        // If in idle mode, just exit idle
        if isInIdleMode {
            await exitIdleMode()
            return
        }

        // Send stop notification to server before stopping
        await sendStopPacket()

        // If server configured idle mode, enter it instead of fully stopping
        if idleIntervalSeconds > 0 {
            await enterIdleMode()
            return
        }

        await fullStop()
    }

    /// Fully stop tracking and idle mode
    private func fullStop() async {
        isRunning = false
        isInIdleMode = false
        idleTask?.cancel()
        idleTask = nil
        keepaliveTask?.cancel()
        keepaliveTask = nil
        assistRequested = false

        // Stop location updates
        locationManager.stopUpdating()
        locationManager.stopIdleLocationMonitoring()

        // Stop battery tracking on main thread (Published properties)
        await MainActor.run {
            batteryMonitor.stopDrainTracking()
        }

        // Clear buffer
        positionBuffer.removeAll()

        // Update state
        statePublisher.send(.idle)

        // Save tracking state on main thread (Published property)
        await MainActor.run {
            preferences.trackingActive = false
        }
    }

    /// Send a stop notification packet to the server
    /// This tells the server the user deliberately stopped tracking (vs losing signal)
    private func sendStopPacket() async {
        guard let position = locationManager.lastPosition else { return }

        sequenceNumber += 1
        let seq = sequenceNumber

        let packet = TrackerPacket(
            id: preferences.sailorId,
            eid: preferences.eventId,
            sq: seq,
            ts: position.unixTimestamp,
            lat: position.latitude,
            lon: position.longitude,
            spd: 0.0,
            hdg: 0,
            ast: false,  // Clear assist on stop
            bat: batteryMonitor.status.level,
            role: preferences.role.rawValue,
            ver: appVersion,
            os: osVersion,
            pwd: preferences.password.isEmpty ? nil : preferences.password,
            chg: batteryMonitor.status.isCharging,
            ps: batteryMonitor.status.isLowPowerMode,
            stopped: true
        )

        // Try up to 5 times with short delays
        for attempt in 1...5 {
            let response = await networkManager.send(packet)
            if response != nil {
                // Got ACK
                return
            }
            // Wait 500ms before retry
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
    }

    // MARK: - Idle Mode

    /// Enter idle mode - stop GPS but keep sending heartbeat packets
    private func enterIdleMode() async {
        isRunning = false
        isInIdleMode = true
        assistRequested = false
        keepaliveTask?.cancel()
        keepaliveTask = nil

        // Stop precise GPS to save battery, but start significant location monitoring
        // to keep the app alive in the background for idle heartbeats
        locationManager.stopUpdating()
        locationManager.startIdleLocationMonitoring()

        // Clear buffer
        positionBuffer.removeAll()

        // Update state
        statePublisher.send(.idleMode)
        statusLinePublisher.send("Idle - waiting for admin")

        print("[TrackerService] Entering idle mode, interval=\(idleIntervalSeconds)s")

        // Send first idle packet immediately so server/WebUI updates right away
        await sendIdlePacket()

        // Start heartbeat loop
        let interval = idleIntervalSeconds
        idleTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(interval) * 1_000_000_000)
                guard !Task.isCancelled else { break }
                await self?.sendIdlePacket()
            }
        }
    }

    /// Send a lightweight idle heartbeat packet
    private func sendIdlePacket() async {
        sequenceNumber += 1
        let seq = sequenceNumber

        let battery = batteryMonitor.status
        preferences.ensureSailorId()

        let packet = TrackerPacket(
            id: preferences.sailorId,
            eid: preferences.eventId,
            sq: seq,
            ts: Int(Date().timeIntervalSince1970),
            lat: nil,
            lon: nil,
            spd: 0.0,
            hdg: 0,
            ast: false,
            bat: battery.level,
            role: preferences.role.rawValue,
            ver: appVersion,
            os: osVersion,
            pwd: preferences.password.isEmpty ? nil : preferences.password,
            chg: battery.isCharging,
            ps: battery.isLowPowerMode,
            idle: true
        )

        let response = await networkManager.send(packet)
        if let response = response {
            await handleACK(response)
        }
    }

    /// Seconds since last position send (returns large value if never sent)
    private func timeSinceLastSend() -> Double {
        guard let lastSend = lastSendTime else { return .greatestFiniteMagnitude }
        return Date().timeIntervalSince(lastSend)
    }

    /// Send a GPS-wait heartbeat packet (tracking active but no GPS fix yet)
    private func sendGpsWaitPacket() async {
        sequenceNumber += 1
        let seq = sequenceNumber

        let battery = batteryMonitor.status
        preferences.ensureSailorId()

        let packet = TrackerPacket(
            id: preferences.sailorId,
            eid: preferences.eventId,
            sq: seq,
            ts: Int(Date().timeIntervalSince1970),
            lat: nil,
            lon: nil,
            spd: 0.0,
            hdg: 0,
            ast: false,
            bat: battery.level,
            role: preferences.role.rawValue,
            ver: appVersion,
            os: osVersion,
            pwd: preferences.password.isEmpty ? nil : preferences.password,
            chg: battery.isCharging,
            ps: battery.isLowPowerMode,
            nsats: 0
        )

        let response = await networkManager.send(packet)
        if let response = response {
            await handleACK(response)
        }
    }

    /// Resume tracking from idle mode (triggered by admin "start" command)
    private func resumeFromIdle() async {
        print("[TrackerService] Resuming from idle mode (remote start)")
        idleTask?.cancel()
        idleTask = nil
        isInIdleMode = false
        locationManager.stopIdleLocationMonitoring()

        remoteStartPublisher.send()

        // Start tracking again
        do {
            try await start()
        } catch {
            print("[TrackerService] Failed to resume from idle: \(error)")
            await fullStop()
        }
    }

    /// Exit idle mode completely (triggered by admin "shutdown" command or user stop)
    public func exitIdleMode() async {
        print("[TrackerService] Exiting idle mode (shutdown)")
        idleTask?.cancel()
        idleTask = nil
        isInIdleMode = false
        isRunning = false
        locationManager.stopIdleLocationMonitoring()

        // Stop battery tracking on main thread (Published properties)
        await MainActor.run {
            batteryMonitor.stopDrainTracking()
        }

        // Update state
        statePublisher.send(.idle)

        // Save tracking state on main thread (Published property)
        await MainActor.run {
            preferences.trackingActive = false
        }

        remoteShutdownPublisher.send()
    }

    /// Check if in idle mode
    public var isIdle: Bool {
        isInIdleMode
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

    /// Check if we received an ACK in the last 60 seconds (for tracker beep feature)
    public var hasRecentAck: Bool {
        guard let time = lastAckTime else { return false }
        return Date().timeIntervalSince(time) < 60
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

        // 1Hz mode - buffer positions and send every 10 seconds
        positionBuffer.append(position)

        // Send when we have accumulated enough time (10 seconds)
        let shouldSend: Bool
        if let lastSend = lastSendTime {
            shouldSend = Date().timeIntervalSince(lastSend) >= 10.0
        } else {
            // First packet - send immediately for quick ACK
            shouldSend = true
        }

        if shouldSend && !positionBuffer.isEmpty {
            await sendPositionBatch()
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
            lastAckTime = Date()  // Update for tracker beep feature

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

        // Update assist enabled state (defaults to true if not specified)
        let assistEnabled = response.isAssistEnabled
        assistEnabledPublisher.send(assistEnabled)

        // Clear local assist flag if server says assist is disabled
        if !assistEnabled && assistRequested {
            assistRequested = false
            print("[TrackerService] Assist cleared by server (assist disabled for event)")
        }

        // Parse idle interval from server
        if let interval = response.idle {
            idleIntervalSeconds = interval
            // If server sent idle=0 while we're in idle mode, shut down
            if interval == 0 && isInIdleMode {
                print("[TrackerService] Server sent idle=0 while in idle mode, shutting down")
                await exitIdleMode()
                return
            }
        }

        // Check for remote stop command
        if response.isStopCommand {
            print("[TrackerService] Received remote STOP command from server")
            // Always notify UI so it can stop buzz timer, live activity, etc.
            remoteStopPublisher.send()
            await stop()
        }

        // Check for remote start command (resume from idle)
        if response.isStartCommand && isInIdleMode {
            print("[TrackerService] Received remote START command from server")
            await resumeFromIdle()
        }

        // Check for remote shutdown command (exit idle)
        if response.isShutdownCommand && isInIdleMode {
            print("[TrackerService] Received remote SHUTDOWN command from server")
            await exitIdleMode()
        }

        // Check for remote cancel assist command
        if response.isCancelAssistCommand {
            print("[TrackerService] Received remote CANCEL ASSIST command from server")
            assistRequested = false
            remoteCancelAssistPublisher.send()
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
            usingHttpFallback: false,  // Will update from network manager
            lastAckTime: lastAckTime
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
