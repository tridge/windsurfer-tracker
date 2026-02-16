import Foundation
import Combine
import CoreLocation
import UIKit
import HealthKit
import ActivityKit
import AudioToolbox
import AVFoundation

/// View model bridging TrackerService to SwiftUI
@MainActor
public class TrackerViewModel: ObservableObject {
    // MARK: - Tracking State

    @Published public var isTracking = false
    @Published public var isIdleMode = false
    @Published public var assistRequested = false
    @Published public var lastPosition: TrackerPosition?
    @Published public var connectionStatus = ConnectionStatus()
    @Published public var eventName = ""
    @Published public var statusLine = "---"  // GPS wait, connecting..., auth failure, or event name
    @Published public var assistEnabled = true  // Whether assist button should be shown
    @Published public var errorMessage: String?
    @Published public var showError = false
    @Published public var totalDistanceMeters: Double = 0

    // MARK: - Settings (bound to PreferencesManager)

    @Published public var sailorId: String
    @Published public var serverHost: String
    @Published public var serverPort: Int
    @Published public var role: TrackerRole
    @Published public var password: String
    @Published public var eventId: Int
    @Published public var trackerBeep: Bool

    // MARK: - HealthKit

    private let healthStore = HKHealthStore()
    private var workoutBuilder: HKWorkoutBuilder?
    private var workoutStartTime: Date?
    private var lastDistanceSampleTime: Date?
    @Published public var workoutState: String = ""

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
    private var beepTimer: Timer?
    private var previousPositionForDistance: TrackerPosition?
    private let volumeButtonAssist = VolumeButtonAssist()
    private let assistTonePlayer = AssistTonePlayer()
    private var assistAlarmTimer: Timer?

    // MARK: - Initialization

    public init() {
        // Load initial values from preferences
        self.sailorId = preferences.sailorId
        self.serverHost = preferences.serverHost
        self.serverPort = preferences.serverPort
        self.role = preferences.role
        self.password = preferences.password
        self.eventId = preferences.eventId
        self.trackerBeep = preferences.trackerBeep

        setupBindings()
        setupVolumeAssist()

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

        $trackerBeep
            .dropFirst()
            .sink { [weak self] value in
                self?.preferences.trackerBeep = value
            }
            .store(in: &cancellables)

        // Subscribe to tracker state
        TrackerService.shared.statePublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] state in
                guard let self = self else { return }
                self.isTracking = state.isTracking
                self.isIdleMode = state.isIdleMode

                // Start/stop volume button assist with tracking
                if state.isTracking || state.isIdleMode {
                    self.volumeButtonAssist.start()
                } else {
                    self.volumeButtonAssist.stop()
                }
            }
            .store(in: &cancellables)

        // Subscribe to position updates
        TrackerService.shared.positionPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] position in
                guard let self = self else { return }
                // Calculate distance from previous position
                if let prevPos = self.previousPositionForDistance {
                    let prevLocation = CLLocation(latitude: prevPos.latitude, longitude: prevPos.longitude)
                    let newLocation = CLLocation(latitude: position.latitude, longitude: position.longitude)
                    let distance = newLocation.distance(from: prevLocation)
                    // Filter out GPS noise (too small) and jumps (too large)
                    if distance > 0.1 && distance < 500 {
                        self.totalDistanceMeters += distance
                        // Log distance to HealthKit
                        self.addDistanceSampleToWorkout(distance)
                    }
                }
                self.previousPositionForDistance = position
                self.lastPosition = position
                // Update Live Activity with new position
                self.updateLiveActivity()
            }
            .store(in: &cancellables)

        // Subscribe to connection status
        TrackerService.shared.connectionStatusPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] status in
                self?.connectionStatus = status
                self?.updateLiveActivity()
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
                self?.updateLiveActivity()
            }
            .store(in: &cancellables)

        // Subscribe to assist enabled status
        TrackerService.shared.assistEnabledPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] enabled in
                self?.assistEnabled = enabled
            }
            .store(in: &cancellables)

        // Subscribe to remote stop commands
        TrackerService.shared.remoteStopPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self = self else { return }
                self.isTracking = false
                self.playTrackingTone(ascending: false)
                self.stopBeepTimer()
                self.stopAssistAlarm()
                self.endLiveActivity()
            }
            .store(in: &cancellables)

        // Subscribe to remote cancel assist commands
        TrackerService.shared.remoteCancelAssistPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self = self else { return }
                self.assistRequested = false
                self.stopAssistAlarm()
                self.playAssistTones(ascending: false)
                // Only show alert if no other dialog is showing
                if !self.showStopConfirmation && !self.showSettings {
                    self.errorMessage = "Assist cancelled by admin"
                    self.showError = true
                }
            }
            .store(in: &cancellables)

        // Subscribe to remote start commands (resume from idle)
        TrackerService.shared.remoteStartPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self = self else { return }
                self.playTrackingTone(ascending: true)
                self.startBeepTimer()
                self.startLiveActivity()
            }
            .store(in: &cancellables)

        // Subscribe to remote shutdown commands (exit idle mode)
        TrackerService.shared.remoteShutdownPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self = self else { return }
                self.playTrackingTone(ascending: false)
                self.stopBeepTimer()
                self.endLiveActivity()
            }
            .store(in: &cancellables)

        // Subscribe to errors
        TrackerService.shared.errorPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] error in
                guard let self = self else { return }
                // Don't show error if another dialog is already showing
                if !self.showStopConfirmation && !self.showSettings {
                    self.errorMessage = error.localizedDescription
                    self.showError = true
                }
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

    // MARK: - Volume Button Assist

    private func setupVolumeAssist() {
        volumeButtonAssist.onComboDetected = { [weak self] in
            guard let self = self else { return }
            NSLog("[VolumeAssist] Combo detected, toggling assist")
            AudioServicesPlaySystemSound(kSystemSoundID_Vibrate)
            self.toggleAssist()
        }
    }

    // MARK: - Assist Tones

    /// Play 2-tone ascending (start) or descending (stop) tracking feedback at max volume
    private func playTrackingTone(ascending: Bool) {
        let savedVolume = AVAudioSession.sharedInstance().outputVolume
        volumeButtonAssist.setSystemVolume(1.0)
        assistTonePlayer.playDouble(ascending: ascending)
        // Restore volume after tones finish (~400ms for 2 tones)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.volumeButtonAssist.setSystemVolume(savedVolume)
        }
    }

    /// Play ascending (activate) or descending (deactivate) assist tones at max volume
    private func playAssistTones(ascending: Bool) {
        // Save current volume, crank to max, play, then restore
        let savedVolume = AVAudioSession.sharedInstance().outputVolume
        volumeButtonAssist.setSystemVolume(1.0)

        assistTonePlayer.play(ascending: ascending)

        // Restore volume after tones finish (~600ms)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) { [weak self] in
            self?.volumeButtonAssist.setSystemVolume(savedVolume)
        }
    }

    /// Start 5-second repeating alarm while assist is active
    private func startAssistAlarm() {
        stopAssistAlarm()
        assistAlarmTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            guard let self = self, self.assistRequested else {
                self?.stopAssistAlarm()
                return
            }
            self.playAssistTones(ascending: true)
        }
        RunLoop.main.add(assistAlarmTimer!, forMode: .common)
    }

    /// Stop the assist alarm timer
    private func stopAssistAlarm() {
        assistAlarmTimer?.invalidate()
        assistAlarmTimer = nil
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

        // Reset distance tracking
        totalDistanceMeters = 0
        previousPositionForDistance = nil

        // Play ascending 2-tone to confirm start
        playTrackingTone(ascending: true)

        Task {
            do {
                try await TrackerService.shared.start()
                // Start tracker beep timer (first beep after 60 seconds)
                startBeepTimer()
                // Start HealthKit workout session
                await startWorkoutSession()
                // Start Live Activity for lock screen status (iOS 16.2+)
                startLiveActivity()
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
        // Play descending 2-tone to confirm stop
        playTrackingTone(ascending: false)

        // Clear event name on stop
        eventName = ""

        // Stop tracker beep timer
        stopBeepTimer()

        // Stop assist alarm
        stopAssistAlarm()

        // End Live Activity
        endLiveActivity()

        Task {
            // End HealthKit workout session
            // Add final energy sample based on total duration
            if let startTime = workoutStartTime {
                let duration = Date().timeIntervalSince(startTime)
                addEnergySampleToWorkout(durationSeconds: duration)
            }
            await endWorkoutSession()
            // stop() will enter idle mode if server configured it
            await TrackerService.shared.stop()
            assistRequested = false
        }
    }

    public func toggleAssist() {
        let activating = !assistRequested
        // If activating assist and not currently tracking, start tracking first
        if activating && !isTracking {
            startTracking()
        }
        Task {
            await TrackerService.shared.toggleAssist()
            assistRequested = await TrackerService.shared.isAssistRequested
        }

        // Play tones and manage alarm
        playAssistTones(ascending: activating)
        if activating {
            startAssistAlarm()
        } else {
            stopAssistAlarm()
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

    /// Fetch event name for current event ID and update eventName
    public func fetchEventName() async {
        // Only fetch if we don't already have the event name
        guard eventName.isEmpty else { return }

        let networkManager = NetworkManager()
        await networkManager.configure(
            host: serverHost,
            port: UInt16(serverPort)
        )
        let fetchedEvents = await networkManager.fetchEvents()

        // Find matching event and update eventName
        if let event = fetchedEvents.first(where: { $0.eid == eventId }) {
            await MainActor.run {
                self.eventName = event.name
            }
        }
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

    // MARK: - Tracker Beep

    private func startBeepTimer() {
        // Cancel any existing timer
        beepTimer?.invalidate()

        NSLog("BEEP: startBeepTimer called")

        // Schedule beep every 60 seconds (first beep after 60 seconds)
        beepTimer = Timer.scheduledTimer(withTimeInterval: 60.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                NSLog("BEEP: timer fired")
                self?.playTrackerBeep()
            }
        }
        // Ensure timer runs even when scrolling or app is in background
        RunLoop.main.add(beepTimer!, forMode: .common)
    }

    private func stopBeepTimer() {
        NSLog("BEEP: stopBeepTimer called")
        beepTimer?.invalidate()
        beepTimer = nil
    }

    private func playTrackerBeep() {
        guard preferences.trackerBeep else {
            NSLog("BEEP: trackerBeep pref is OFF, skipping")
            return
        }

        Task {
            let hasRecentAck = await TrackerService.shared.hasRecentAck
            NSLog("BEEP: vibrating, hasRecentAck=%d", hasRecentAck ? 1 : 0)

            if hasRecentAck {
                // One vibration - connection OK
                vibrate()
            } else {
                // Two vibrations - no connection
                vibrate()
                try? await Task.sleep(nanoseconds: 300_000_000)  // 300ms gap
                vibrate()
            }
        }
    }

    /// Trigger device vibration - works on all iPhones including SE
    private func vibrate() {
        // Use system vibration which works on all devices
        // kSystemSoundID_Vibrate (4095) works on all iPhones
        AudioServicesPlaySystemSound(kSystemSoundID_Vibrate)
    }

    // MARK: - HealthKit Workout

    /// Request HealthKit authorization for workouts
    public func requestHealthKitAuthorization() async -> Bool {
        guard HKHealthStore.isHealthDataAvailable() else {
            print("[HealthKit] Health data not available on this device")
            return false
        }

        let workoutType = HKObjectType.workoutType()
        let activeEnergyType = HKQuantityType.quantityType(forIdentifier: .activeEnergyBurned)!
        let distanceType = HKQuantityType.quantityType(forIdentifier: .distanceWalkingRunning)!

        do {
            try await healthStore.requestAuthorization(
                toShare: [workoutType, activeEnergyType, distanceType],
                read: [activeEnergyType, distanceType]
            )
            // Check actual authorization status
            let workoutAuth = healthStore.authorizationStatus(for: workoutType)
            print("[HealthKit] Authorization request completed. Workout status: \(workoutAuth.rawValue)")
            // Note: authorizationStatus only tells us about read access, not write
            // For write access, we just have to try and see if it fails
            return true
        } catch {
            print("[HealthKit] Authorization failed: \(error.localizedDescription)")
            return false
        }
    }

    /// Start a workout session for tracking
    private func startWorkoutSession() async {
        // Request authorization first
        let authorized = await requestHealthKitAuthorization()
        guard authorized else {
            workoutState = "not authorized"
            return
        }

        // End any existing workout
        await endWorkoutSession()

        // Create workout configuration for sailing/water sports
        let configuration = HKWorkoutConfiguration()
        configuration.activityType = .sailing  // Closest to windsurfing
        configuration.locationType = .outdoor

        do {
            // Create workout builder (iOS doesn't use HKWorkoutSession like watchOS)
            let builder = HKWorkoutBuilder(
                healthStore: healthStore,
                configuration: configuration,
                device: .local()
            )

            workoutBuilder = builder
            workoutStartTime = Date()
            lastDistanceSampleTime = workoutStartTime

            try await builder.beginCollection(at: workoutStartTime!)
            workoutState = "running"
            print("[HealthKit] Workout session started")
        } catch {
            print("[HealthKit] Failed to start workout: \(error.localizedDescription)")
            workoutState = "error"
        }
    }

    /// End the current workout session and save to HealthKit
    private func endWorkoutSession() async {
        guard let builder = workoutBuilder, let startTime = workoutStartTime else {
            print("[HealthKit] endWorkoutSession: No active workout to end")
            return
        }

        print("[HealthKit] Ending workout session...")
        let endTime = Date()

        do {
            try await builder.endCollection(at: endTime)

            // Finish and save the workout
            if let workout = try await builder.finishWorkout() {
                let duration = workout.duration
                let distance = workout.totalDistance?.doubleValue(for: .meter()) ?? 0
                print("[HealthKit] Workout saved: \(String(format: "%.0f", duration))s, \(String(format: "%.0f", distance))m")
                workoutState = "saved"
            }
        } catch {
            print("[HealthKit] Failed to save workout: \(error.localizedDescription)")
            workoutState = "save failed"
        }

        workoutBuilder = nil
        workoutStartTime = nil
        lastDistanceSampleTime = nil
    }

    /// Add a distance sample to the workout
    private func addDistanceSampleToWorkout(_ distance: Double) {
        guard let builder = workoutBuilder else { return }

        let distanceType = HKQuantityType.quantityType(forIdentifier: .distanceWalkingRunning)!
        let distanceQuantity = HKQuantity(unit: .meter(), doubleValue: distance)
        let now = Date()

        let sample = HKQuantitySample(
            type: distanceType,
            quantity: distanceQuantity,
            start: lastDistanceSampleTime ?? now,
            end: now
        )
        lastDistanceSampleTime = now

        builder.add([sample]) { success, error in
            if let error = error {
                print("[HealthKit] Failed to add distance sample: \(error.localizedDescription)")
            }
        }
    }

    /// Add an active energy sample to the workout
    /// Estimated based on MET value for sailing (~3.0) and duration
    private func addEnergySampleToWorkout(durationSeconds: TimeInterval) {
        guard let builder = workoutBuilder else { return }

        // MET for sailing is approximately 3.0
        // Calories = MET * weight(kg) * duration(hours)
        // Using approximate 70kg average weight
        let metValue = 3.0
        let weightKg = 70.0
        let hours = durationSeconds / 3600.0
        let kilocalories = metValue * weightKg * hours

        let energyType = HKQuantityType.quantityType(forIdentifier: .activeEnergyBurned)!
        let energyQuantity = HKQuantity(unit: .kilocalorie(), doubleValue: kilocalories)
        let now = Date()

        let sample = HKQuantitySample(
            type: energyType,
            quantity: energyQuantity,
            start: workoutStartTime ?? now,
            end: now
        )

        builder.add([sample]) { success, error in
            if let error = error {
                print("[HealthKit] Failed to add energy sample: \(error.localizedDescription)")
            }
        }
    }

    // MARK: - Live Activity

    /// Start Live Activity for lock screen tracking status (iOS 16.2+)
    private func startLiveActivity() {
        if #available(iOS 16.2, *) {
            LiveActivityManager.shared.startActivity(
                sailorId: sailorId,
                eventId: eventId
            )
        }
    }

    /// End Live Activity
    private func endLiveActivity() {
        if #available(iOS 16.2, *) {
            LiveActivityManager.shared.endActivity()
        }
    }

    /// Update Live Activity with current state
    private func updateLiveActivity() {
        guard isTracking else { return }

        if #available(iOS 16.2, *) {
            // Determine if connected based on recent ACK (within 30 seconds)
            let isConnected = connectionStatus.lastAckTime.map {
                Date().timeIntervalSince($0) < 30
            } ?? false

            LiveActivityManager.shared.updateActivity(
                isConnected: isConnected,
                speedKnots: lastPosition?.speedKnots ?? 0,
                ackRatePercent: Int(connectionStatus.ackRate),
                statusLine: statusLine,
                assistActive: assistRequested
            )
        }
    }
}
