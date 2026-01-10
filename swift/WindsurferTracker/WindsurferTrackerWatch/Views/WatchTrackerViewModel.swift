import Foundation
import Combine
import WatchKit
import HealthKit
import AVFoundation
import CoreMotion

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

    // MARK: - Fitness Metrics (for UI display and HealthKit)

    @Published public var totalDistance: Double = 0  // meters
    @Published public var currentHeartRate: Int = 0  // bpm
    private var lastDistanceUpdate: Date?
    private var workoutStartTime: Date?

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

    @Published public var trackerBeep: Bool {
        didSet { preferences.trackerBeep = trackerBeep }
    }

    @Published public var raceTimerEnabled: Bool {
        didSet { preferences.raceTimerEnabled = raceTimerEnabled }
    }

    @Published public var raceTimerMinutes: Int {
        didSet { preferences.raceTimerMinutes = raceTimerMinutes }
    }

    @Published public var raceTimerTapGForce: Int {
        didSet { preferences.raceTimerTapGForce = raceTimerTapGForce }
    }

    // MARK: - Race Timer State

    @Published public var countdownSeconds: Int? = nil  // nil = not active
    private var countdownTargetTime: Date? = nil
    private var countdownTimer: Timer? = nil
    private var lastAnnouncedSecond: Int = -1
    private let ttsLatencySeconds: TimeInterval = 0.25  // Announce early to compensate for TTS delay

    // Audio playback
    private var audioPlayer: AVAudioPlayer?

    // Tap detection (accelerometer)
    private let motionManager = CMMotionManager()
    private var lastTapTime: Date = .distantPast
    private var tapThreshold: Double {
        Double(raceTimerTapGForce) * 9.81  // Convert g-force to m/s²
    }
    private let tapCooldown: TimeInterval = 1.0
    private let gravity: Double = 9.81

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
    private var beepTimer: Timer?

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
        self.trackerBeep = prefs.trackerBeep
        self.raceTimerEnabled = prefs.raceTimerEnabled
        self.raceTimerMinutes = prefs.raceTimerMinutes
        self.raceTimerTapGForce = prefs.raceTimerTapGForce

        super.init()
        setupBindings()
        setupAudioSession()
    }

    private func setupAudioSession() {
        do {
            let session = AVAudioSession.sharedInstance()

            // Use .playback category with .voicePrompt mode
            // .voicePrompt tells watchOS this is voice content meant for the speaker
            // This is what AVSpeechSynthesizer used and routes to built-in speaker
            try session.setCategory(.playback, mode: .voicePrompt, options: [])
            try session.setActive(true)

            print("[AUDIO] Audio session configured with voicePrompt mode")
        } catch {
            print("[AUDIO] Failed to setup audio session: \(error.localizedDescription)")
        }
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
                self?.updateDistance(from: self?.lastPosition, to: position)
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

        // Subscribe to heart rate updates to add samples to workout
        HeartRateMonitor.shared.$currentHeartRate
            .compactMap { $0 }  // Filter nil values
            .receive(on: DispatchQueue.main)
            .sink { [weak self] heartRate in
                guard let self = self, self.isTracking else { return }
                self.addHeartRateSampleToWorkout(heartRate)
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

                // Start tracker beep timer (first beep after 60 seconds)
                startBeepTimer()

                // Start tap detection for race timer
                startTapDetection()

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
        let distanceType = HKQuantityType.quantityType(forIdentifier: .distanceWalkingRunning)!

        do {
            try await healthStore.requestAuthorization(
                toShare: [workoutType, activeEnergyType, distanceType, heartRateType],
                read: [heartRateType, activeEnergyType, distanceType]
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
        let startTime = Date()
        workoutStartTime = startTime
        lastDistanceUpdate = startTime
        session.startActivity(with: startTime)
        try await builder.beginCollection(at: startTime)
        print("[WORKOUT] Workout session started successfully")
    }

    private func stopWorkoutSession() async {
        guard let session = workoutSession, let builder = workoutBuilder else { return }

        // End workout
        session.end()

        do {
            try await builder.endCollection(at: Date())
            if let workout = try await builder.finishWorkout() {
                print("[WORKOUT] Workout saved to Health: \(workout.duration)s, \(workout.totalDistance?.doubleValue(for: .meter()) ?? 0)m")
            }
        } catch {
            print("[WORKOUT] Failed to save workout: \(error.localizedDescription)")
        }

        workoutSession = nil
        workoutBuilder = nil
    }

    // MARK: - Fitness Data Collection

    /// Calculate and accumulate distance from GPS position updates
    private func updateDistance(from oldPosition: TrackerPosition?, to newPosition: TrackerPosition?) {
        guard isTracking,
              let old = oldPosition,
              let new = newPosition,
              old.latitude != 0, old.longitude != 0,
              new.latitude != 0, new.longitude != 0 else { return }

        // Calculate distance using Haversine formula
        let distance = haversineDistance(
            lat1: old.latitude, lon1: old.longitude,
            lat2: new.latitude, lon2: new.longitude
        )

        // Only add reasonable distances (filter GPS noise)
        if distance > 0.5 && distance < 500 {  // Between 0.5m and 500m
            totalDistance += distance
            addDistanceSampleToWorkout(distance)
        }
    }

    /// Haversine formula to calculate distance between two GPS points in meters
    private func haversineDistance(lat1: Double, lon1: Double, lat2: Double, lon2: Double) -> Double {
        let R = 6371000.0  // Earth radius in meters
        let dLat = (lat2 - lat1) * .pi / 180
        let dLon = (lon2 - lon1) * .pi / 180
        let a = sin(dLat/2) * sin(dLat/2) +
                cos(lat1 * .pi / 180) * cos(lat2 * .pi / 180) *
                sin(dLon/2) * sin(dLon/2)
        let c = 2 * atan2(sqrt(a), sqrt(1-a))
        return R * c
    }

    /// Add distance sample to workout builder
    private func addDistanceSampleToWorkout(_ distance: Double) {
        guard let builder = workoutBuilder else { return }

        let distanceType = HKQuantityType.quantityType(forIdentifier: .distanceWalkingRunning)!
        let distanceQuantity = HKQuantity(unit: .meter(), doubleValue: distance)
        let now = Date()
        let sample = HKQuantitySample(
            type: distanceType,
            quantity: distanceQuantity,
            start: lastDistanceUpdate ?? now,
            end: now
        )
        lastDistanceUpdate = now

        builder.add([sample]) { success, error in
            if let error = error {
                print("[WORKOUT] Failed to add distance sample: \(error.localizedDescription)")
            }
        }
    }

    /// Add heart rate sample to workout builder (called from HeartRateMonitor updates)
    public func addHeartRateSampleToWorkout(_ heartRate: Int) {
        guard let builder = workoutBuilder, heartRate > 0 else { return }

        // Update published value for UI
        Task { @MainActor in
            self.currentHeartRate = heartRate
        }

        let heartRateType = HKQuantityType.quantityType(forIdentifier: .heartRate)!
        let heartRateQuantity = HKQuantity(unit: HKUnit.count().unitDivided(by: .minute()), doubleValue: Double(heartRate))
        let now = Date()
        let sample = HKQuantitySample(
            type: heartRateType,
            quantity: heartRateQuantity,
            start: now,
            end: now
        )

        builder.add([sample]) { success, error in
            if let error = error {
                print("[WORKOUT] Failed to add heart rate sample: \(error.localizedDescription)")
            }
        }
    }

    public func stopTracking() {
        // Stop tracker beep timer
        stopBeepTimer()

        // Stop tap detection and reset countdown
        stopTapDetection()
        if isCountdownRunning {
            countdownTimer?.invalidate()
            countdownTimer = nil
            countdownSeconds = nil
            countdownTargetTime = nil
        }

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
            totalDistance = 0  // Reset fitness metrics
            currentHeartRate = 0
            lastDistanceUpdate = nil
            workoutStartTime = nil
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

    // MARK: - Tracker Beep

    private func startBeepTimer() {
        // Cancel any existing timer
        beepTimer?.invalidate()

        // Schedule beep every 60 seconds (first beep after 60 seconds)
        beepTimer = Timer.scheduledTimer(withTimeInterval: 60.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.playTrackerBeep()
            }
        }
    }

    private func stopBeepTimer() {
        beepTimer?.invalidate()
        beepTimer = nil
    }

    private func playTrackerBeep() {
        guard preferences.trackerBeep else { return }

        Task {
            let hasRecentAck = await TrackerService.shared.hasRecentAck
            let device = WKInterfaceDevice.current()

            if hasRecentAck {
                // One buzz - connection OK
                device.play(.click)
            } else {
                // Two buzzes - no connection
                device.play(.click)
                try? await Task.sleep(nanoseconds: 150_000_000)  // 150ms
                device.play(.click)
            }
        }
    }

    // MARK: - Race Timer

    private func speak(_ text: String) {
        print("[AUDIO] Playing: \"\(text)\"")

        // Map text to audio filename
        let filename: String
        switch text {
        case "9 minutes": filename = "9_minutes"
        case "8 minutes": filename = "8_minutes"
        case "7 minutes": filename = "7_minutes"
        case "6 minutes": filename = "6_minutes"
        case "5 minutes": filename = "5_minutes"
        case "4 minutes": filename = "4_minutes"
        case "3 minutes": filename = "3_minutes"
        case "2 minutes": filename = "2_minutes"
        case "1 minute": filename = "1_minute"
        case "30 seconds": filename = "30_seconds"
        case "20 seconds": filename = "20_seconds"
        case "10": filename = "10"
        case "9": filename = "9"
        case "8": filename = "8"
        case "7": filename = "7"
        case "6": filename = "6"
        case "5": filename = "5"
        case "4": filename = "4"
        case "3": filename = "3"
        case "2": filename = "2"
        case "1": filename = "1"
        case "Start!": filename = "start"
        case "reset": filename = "reset"
        default:
            print("[AUDIO] No audio file for: \"\(text)\"")
            return
        }

        // Load audio file from bundle
        // Try with subdirectory first
        var url = Bundle.main.url(forResource: filename, withExtension: "m4a", subdirectory: "Audio")

        // If not found, try without subdirectory
        if url == nil {
            url = Bundle.main.url(forResource: filename, withExtension: "m4a")
        }

        // If still not found, try looking in Resources/Audio
        if url == nil {
            url = Bundle.main.url(forResource: filename, withExtension: "m4a", subdirectory: "Resources/Audio")
        }

        guard let audioURL = url else {
            print("[AUDIO] Audio file not found: \(filename).m4a")
            print("[AUDIO] Bundle path: \(Bundle.main.bundlePath)")
            print("[AUDIO] Bundle resources: \(Bundle.main.paths(forResourcesOfType: "m4a", inDirectory: nil))")
            return
        }

        // Stop any currently playing audio
        audioPlayer?.stop()

        do {
            // Create AVAudioPlayer for audio playback
            let player = try AVAudioPlayer(contentsOf: audioURL)
            audioPlayer = player
            player.prepareToPlay()
            player.play()
            print("[AUDIO] ✓ Playing \(filename).m4a with AVAudioPlayer")
        } catch {
            print("[AUDIO] Error creating AVAudioPlayer: \(error.localizedDescription)")
        }
    }

    /// Start the race countdown timer
    public func startCountdown() {
        let minutes = raceTimerMinutes
        countdownSeconds = minutes * 60
        countdownTargetTime = Date().addingTimeInterval(TimeInterval(minutes * 60))
        lastAnnouncedSecond = minutes * 60  // Prevent duplicate announcement at same second

        // Announce start
        speak("\(minutes) minute\(minutes > 1 ? "s" : "")")

        // Start high-frequency timer for accurate timing
        countdownTimer?.invalidate()
        countdownTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.runCountdownTick()
            }
        }

        print("[TIMER] Race countdown started: \(minutes) minutes")
    }

    /// Reset/cancel the race countdown timer
    public func resetCountdown() {
        countdownTimer?.invalidate()
        countdownTimer = nil
        countdownSeconds = nil
        countdownTargetTime = nil
        lastAnnouncedSecond = -1
        speak("reset")
        print("[TIMER] Race countdown reset")
    }

    /// Check if countdown is currently running
    public var isCountdownRunning: Bool {
        countdownSeconds != nil && countdownTargetTime != nil
    }

    private func runCountdownTick() {
        guard let targetTime = countdownTargetTime else { return }

        let now = Date()
        let msRemaining = targetTime.timeIntervalSince(now) * 1000

        if msRemaining <= -500 {
            // Past the start time
            countdownTimer?.invalidate()
            countdownTimer = nil
            countdownSeconds = nil
            countdownTargetTime = nil
            return
        }

        // Calculate seconds for display (round to nearest)
        let displaySeconds = max(0, Int((msRemaining + 500) / 1000))
        if displaySeconds != countdownSeconds {
            countdownSeconds = displaySeconds
        }

        // Announce early to compensate for TTS latency
        let adjustedMs = msRemaining - (ttsLatencySeconds * 1000)
        let announceSecond = Int(ceil(adjustedMs / 1000))

        if announceSecond != lastAnnouncedSecond && announceSecond >= 0 {
            if announceSecond == 0 {
                announceStart()
            } else {
                announceCountdownIfNeeded(announceSecond)
            }
            lastAnnouncedSecond = announceSecond
        }
    }

    private func announceCountdownIfNeeded(_ seconds: Int) {
        let device = WKInterfaceDevice.current()

        switch seconds {
        case let s where s % 60 == 0 && s > 0:
            // Each minute
            let minutes = s / 60
            speak("\(minutes) minute\(minutes > 1 ? "s" : "")")
            device.play(.notification)

        case 30:
            speak("30 seconds")
            device.play(.notification)

        case 20:
            speak("20 seconds")
            device.play(.notification)

        case 1...10:
            // Final 10 seconds
            speak("\(seconds)")
            device.play(.click)

        default:
            break
        }
    }

    private func announceStart() {
        speak("Start!")
        let device = WKInterfaceDevice.current()
        // Triple buzz for start
        device.play(.notification)
        Task {
            try? await Task.sleep(nanoseconds: 200_000_000)
            device.play(.notification)
            try? await Task.sleep(nanoseconds: 200_000_000)
            device.play(.notification)
        }
    }

    // MARK: - Tap Detection (Accelerometer)

    public func startTapDetection() {
        guard raceTimerEnabled else {
            print("[TAP] Race timer not enabled, tap detection disabled")
            return
        }
        guard motionManager.isAccelerometerAvailable else {
            print("[TAP] Accelerometer not available")
            return
        }

        motionManager.accelerometerUpdateInterval = 1.0 / 100.0  // 100 Hz
        motionManager.startAccelerometerUpdates(to: .main) { [weak self] data, error in
            guard let self = self, let data = data else { return }

            let x = data.acceleration.x * 9.81  // Convert to m/s²
            let y = data.acceleration.y * 9.81
            let z = data.acceleration.z * 9.81

            // Calculate magnitude and subtract gravity
            let magnitude = sqrt(x*x + y*y + z*z)
            let accelAboveGravity = abs(magnitude - self.gravity)

            // Detect tap (acceleration spike above gravity)
            if accelAboveGravity > self.tapThreshold {
                let now = Date()
                if now.timeIntervalSince(self.lastTapTime) > self.tapCooldown {
                    self.lastTapTime = now
                    print("[TAP] Tap detected! Acceleration: \(String(format: "%.1f", accelAboveGravity)) m/s² (threshold: \(String(format: "%.1f", self.tapThreshold)))")
                    self.handleTap()
                }
            }
        }
        print("[TAP] Tap detection started - threshold: \(String(format: "%.1f", tapThreshold)) m/s² (\(raceTimerTapGForce)g)")
    }

    public func stopTapDetection() {
        motionManager.stopAccelerometerUpdates()
        print("[TAP] Tap detection stopped")
    }

    private func handleTap() {
        print("[TAP] Handle tap - countdown running: \(isCountdownRunning)")
        if isCountdownRunning {
            resetCountdown()
        } else {
            startCountdown()
        }
    }

    // MARK: - Action Button (Apple Watch Ultra)

    /// Handle action button press (called from app delegate or extension delegate)
    public func handleActionButton() {
        guard raceTimerEnabled && isTracking else { return }

        if isCountdownRunning {
            resetCountdown()
        } else {
            startCountdown()
        }
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
