import Foundation
import Combine
#if os(iOS)
import UIKit
#elseif os(watchOS)
import WatchKit
#endif

/// Monitor battery level, charging state, and drain rate
public final class BatteryMonitor: ObservableObject {
    public static let shared = BatteryMonitor()

    // MARK: - Published State

    @Published public private(set) var level: Int = -1
    @Published public private(set) var isCharging: Bool = false
    @Published public private(set) var isLowPowerMode: Bool = false
    @Published public private(set) var drainRate: Double? = nil

    // MARK: - Tracking State

    private var trackingStartTime: Date?
    private var trackingStartLevel: Int?
    private var cancellables = Set<AnyCancellable>()

    // MARK: - Initialization

    private init() {
        #if os(iOS)
        setupiOSMonitoring()
        #elseif os(watchOS)
        setupWatchOSMonitoring()
        #endif
    }

    // MARK: - Public Methods

    /// Start tracking battery drain
    public func startDrainTracking() {
        trackingStartTime = Date()
        trackingStartLevel = level >= 0 ? level : nil
        drainRate = nil
    }

    /// Stop tracking battery drain
    public func stopDrainTracking() {
        trackingStartTime = nil
        trackingStartLevel = nil
        drainRate = nil
    }

    /// Get current battery status
    public var status: BatteryStatus {
        BatteryStatus(
            level: level,
            isCharging: isCharging,
            isLowPowerMode: isLowPowerMode,
            drainRate: drainRate
        )
    }

    /// Update drain rate calculation
    public func updateDrainRate() {
        guard let startTime = trackingStartTime,
              let startLevel = trackingStartLevel,
              level >= 0,
              !isCharging else {
            return
        }

        let elapsedMinutes = Date().timeIntervalSince(startTime) / 60

        // Only calculate after minimum tracking time
        guard elapsedMinutes >= TrackerConfig.batteryDrainMinMinutes else {
            return
        }

        let levelDrop = startLevel - level
        guard levelDrop > 0 else {
            drainRate = 0
            return
        }

        // Calculate drain rate in %/hour
        let elapsedHours = elapsedMinutes / 60
        drainRate = Double(levelDrop) / elapsedHours
    }

    // MARK: - iOS Setup

    #if os(iOS)
    private func setupiOSMonitoring() {
        // Enable battery monitoring
        UIDevice.current.isBatteryMonitoringEnabled = true

        // Initial values
        updateiOSBatteryState()

        // Monitor battery level changes
        NotificationCenter.default.publisher(for: UIDevice.batteryLevelDidChangeNotification)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                self?.updateiOSBatteryState()
            }
            .store(in: &cancellables)

        // Monitor charging state changes
        NotificationCenter.default.publisher(for: UIDevice.batteryStateDidChangeNotification)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                self?.updateiOSBatteryState()
            }
            .store(in: &cancellables)

        // Monitor low power mode
        NotificationCenter.default.publisher(for: .NSProcessInfoPowerStateDidChange)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                self?.isLowPowerMode = ProcessInfo.processInfo.isLowPowerModeEnabled
            }
            .store(in: &cancellables)

        isLowPowerMode = ProcessInfo.processInfo.isLowPowerModeEnabled
    }

    private func updateiOSBatteryState() {
        let device = UIDevice.current
        let batteryLevel = device.batteryLevel

        if batteryLevel >= 0 {
            level = Int(batteryLevel * 100)
        } else {
            level = -1
        }

        switch device.batteryState {
        case .charging, .full:
            isCharging = true
        case .unplugged, .unknown:
            isCharging = false
        @unknown default:
            isCharging = false
        }

        updateDrainRate()
    }
    #endif

    // MARK: - watchOS Setup

    #if os(watchOS)
    private func setupWatchOSMonitoring() {
        // watchOS battery monitoring is more limited
        // We need to poll periodically
        Timer.publish(every: 60, on: .main, in: .common)
            .autoconnect()
            .sink { [weak self] _ in
                self?.updateWatchOSBatteryState()
            }
            .store(in: &cancellables)

        updateWatchOSBatteryState()

        // Low power mode
        isLowPowerMode = ProcessInfo.processInfo.isLowPowerModeEnabled

        NotificationCenter.default.publisher(for: .NSProcessInfoPowerStateDidChange)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                self?.isLowPowerMode = ProcessInfo.processInfo.isLowPowerModeEnabled
            }
            .store(in: &cancellables)
    }

    private func updateWatchOSBatteryState() {
        let device = WKInterfaceDevice.current()

        // Enable battery monitoring
        device.isBatteryMonitoringEnabled = true

        let batteryLevel = device.batteryLevel
        if batteryLevel >= 0 {
            level = Int(batteryLevel * 100)
        } else {
            level = -1
        }

        switch device.batteryState {
        case .charging, .full:
            isCharging = true
        case .unplugged, .unknown:
            isCharging = false
        @unknown default:
            isCharging = false
        }

        updateDrainRate()
    }
    #endif
}
