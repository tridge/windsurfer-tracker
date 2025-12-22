import Foundation
import CoreLocation
import Combine

/// CoreLocation wrapper for GPS tracking with background support
public final class LocationManager: NSObject, ObservableObject {
    public static let shared = LocationManager()

    // MARK: - Published State

    @Published public private(set) var authorizationStatus: CLAuthorizationStatus = .notDetermined
    @Published public private(set) var lastLocation: CLLocation?
    @Published public private(set) var lastPosition: TrackerPosition?

    // MARK: - Publishers

    /// Publisher for new location updates
    public let locationPublisher = PassthroughSubject<CLLocation, Never>()

    /// Publisher for position updates (filtered and converted)
    public let positionPublisher = PassthroughSubject<TrackerPosition, Never>()

    /// Publisher for authorization changes
    public let authorizationPublisher = PassthroughSubject<CLAuthorizationStatus, Never>()

    /// Publisher for errors
    public let errorPublisher = PassthroughSubject<Error, Never>()

    // MARK: - Private Properties

    private let locationManager = CLLocationManager()
    private var isUpdating = false
    private var lastSentTimestamp: Date?
    private var backgroundModeConfigured = false
    private var locationTimer: Timer?

    // MARK: - Initialization

    private override init() {
        super.init()
        locationManager.delegate = self
        locationManager.desiredAccuracy = kCLLocationAccuracyBest
        locationManager.distanceFilter = kCLDistanceFilterNone

        #if os(iOS)
        locationManager.activityType = .fitness
        // Note: allowsBackgroundLocationUpdates is set in startUpdating()
        // Setting it here before authorization causes a crash
        #endif

        authorizationStatus = locationManager.authorizationStatus
    }

    // MARK: - Authorization

    /// Request location authorization
    public func requestAuthorization() {
        // Must be called on main thread with slight delay to ensure UI is ready
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [self] in
            // Use the actual CLLocationManager status, not our cached property
            let status = locationManager.authorizationStatus
            authorizationStatus = status  // Sync our property

            switch status {
            case .notDetermined:
                locationManager.requestWhenInUseAuthorization()
            case .authorizedWhenInUse:
                locationManager.requestAlwaysAuthorization()
            default:
                // Already authorized or denied - publish for UI update
                authorizationPublisher.send(status)
            }
        }
    }

    /// Check if we have sufficient authorization for tracking
    public var hasTrackingAuthorization: Bool {
        // Use live status from CLLocationManager
        let status = locationManager.authorizationStatus
        // Also update our cached property
        if status != authorizationStatus {
            DispatchQueue.main.async { [self] in
                authorizationStatus = status
            }
        }
        switch status {
        case .authorizedAlways:
            return true
        case .authorizedWhenInUse:
            // WhenInUse works but background tracking may be limited
            return true
        default:
            return false
        }
    }

    /// Check if Always authorization is granted (best for background)
    public var hasAlwaysAuthorization: Bool {
        authorizationStatus == .authorizedAlways
    }

    // MARK: - Location Updates

    /// Start location updates
    /// - Parameter highFrequency: If true, updates come at 1Hz; otherwise throttled to 10 seconds
    public func startUpdating(highFrequency: Bool = false) {
        guard hasTrackingAuthorization else {
            errorPublisher.send(TrackerError.locationPermissionDenied)
            return
        }

        guard CLLocationManager.locationServicesEnabled() else {
            errorPublisher.send(TrackerError.locationServicesDisabled)
            return
        }

        isUpdating = true
        lastSentTimestamp = nil

        #if os(iOS)
        // Enable background location updates now that we have authorization
        // Only configure once per app session to avoid crashes on restart
        if !backgroundModeConfigured {
            backgroundModeConfigured = true
            locationManager.pausesLocationUpdatesAutomatically = false
            // allowsBackgroundLocationUpdates can crash if capability isn't properly set
            if Bundle.main.object(forInfoDictionaryKey: "UIBackgroundModes") != nil {
                locationManager.allowsBackgroundLocationUpdates = true
                locationManager.showsBackgroundLocationIndicator = true
            }
        }
        #endif

        if highFrequency {
            // 1Hz mode - get updates as fast as possible
            locationManager.desiredAccuracy = kCLLocationAccuracyBest
            locationManager.distanceFilter = kCLDistanceFilterNone
        } else {
            // Standard mode - still get fast updates but we'll throttle sending
            locationManager.desiredAccuracy = kCLLocationAccuracyBest
            locationManager.distanceFilter = kCLDistanceFilterNone
        }

        locationManager.startUpdatingLocation()

        #if targetEnvironment(simulator)
        // Use a timer to restart location updates periodically in simulator
        // This is needed because simulators don't deliver continuous updates
        // and simctl location set only provides a one-time location
        DispatchQueue.main.async { [weak self] in
            self?.locationTimer?.invalidate()
            let interval = highFrequency ? 1.0 : 10.0
            self?.locationTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
                // Stop and restart to force new location delivery
                self?.locationManager.stopUpdatingLocation()
                self?.locationManager.startUpdatingLocation()
            }
        }
        #endif
    }

    /// Stop location updates
    public func stopUpdating() {
        isUpdating = false
        locationManager.stopUpdatingLocation()
        locationTimer?.invalidate()
        locationTimer = nil
    }

    /// Get current location immediately
    public func requestLocation() {
        locationManager.requestLocation()
    }

    // MARK: - Private Methods

    private func processLocation(_ location: CLLocation) {
        lastLocation = location

        // Filter by accuracy
        guard location.horizontalAccuracy > 0,
              location.horizontalAccuracy <= TrackerConfig.maxAccuracyMeters else {
            return
        }

        let position = TrackerPosition(from: location)
        lastPosition = position

        // Publish to subscribers
        locationPublisher.send(location)
        positionPublisher.send(position)
    }
}

// MARK: - CLLocationManagerDelegate

extension LocationManager: CLLocationManagerDelegate {
    public func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last else { return }
        processLocation(location)
    }

    public func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        // Filter out non-critical errors (common in simulator)
        if let clError = error as? CLError {
            switch clError.code {
            case .locationUnknown:
                // Temporary failure - location services will keep trying
                return
            case .denied:
                // Permission denied - this is critical
                errorPublisher.send(TrackerError.locationPermissionDenied)
                return
            default:
                break
            }
        }
        errorPublisher.send(error)
    }

    public func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        authorizationStatus = manager.authorizationStatus
        authorizationPublisher.send(authorizationStatus)

        // If we just got WhenInUse, request Always
        if authorizationStatus == .authorizedWhenInUse {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                manager.requestAlwaysAuthorization()
            }
        }
    }

    // Legacy delegate method for older iOS
    public func locationManager(_ manager: CLLocationManager, didChangeAuthorization status: CLAuthorizationStatus) {
        authorizationStatus = status
        authorizationPublisher.send(status)
    }
}
