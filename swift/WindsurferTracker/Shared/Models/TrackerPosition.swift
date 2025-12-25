import Foundation
import CoreLocation

/// Internal position data model
public struct TrackerPosition: Equatable, Sendable {
    /// Latitude in degrees
    public let latitude: Double

    /// Longitude in degrees
    public let longitude: Double

    /// Speed in knots
    public let speedKnots: Double

    /// Heading in degrees (0-360)
    public let heading: Int

    /// Horizontal accuracy in meters
    public let accuracy: Double

    /// Timestamp of the position
    public let timestamp: Date

    /// Unix timestamp in seconds
    public var unixTimestamp: Int {
        Int(timestamp.timeIntervalSince1970)
    }

    public init(
        latitude: Double,
        longitude: Double,
        speedKnots: Double,
        heading: Int,
        accuracy: Double,
        timestamp: Date
    ) {
        self.latitude = latitude
        self.longitude = longitude
        self.speedKnots = speedKnots
        self.heading = heading
        self.accuracy = accuracy
        self.timestamp = timestamp
    }

    /// Create from CLLocation
    public init(from location: CLLocation) {
        self.latitude = location.coordinate.latitude
        self.longitude = location.coordinate.longitude
        self.speedKnots = GeoCalculations.metersPerSecondToKnots(max(0, location.speed))
        self.heading = Int(location.course >= 0 ? location.course : 0)
        self.accuracy = location.horizontalAccuracy
        self.timestamp = location.timestamp
    }

    /// Check if position meets accuracy requirements
    public var isAccurate: Bool {
        accuracy > 0 && accuracy <= TrackerConfig.maxAccuracyMeters
    }

    /// Formatted latitude string with N/S
    public var formattedLatitude: String {
        let direction = latitude >= 0 ? "N" : "S"
        return String(format: "%.5f°%@", abs(latitude), direction)
    }

    /// Formatted longitude string with E/W
    public var formattedLongitude: String {
        let direction = longitude >= 0 ? "E" : "W"
        return String(format: "%.5f°%@", abs(longitude), direction)
    }

    /// Formatted speed string
    public var formattedSpeed: String {
        String(format: "%.1f kts", speedKnots)
    }

    /// Formatted heading string
    public var formattedHeading: String {
        String(format: "%d°", heading)
    }

    /// Convert to array for 1Hz mode batch: [timestamp, lat, lon, speed]
    public func toPositionArray() -> [Double] {
        [Double(unixTimestamp), latitude, longitude, (speedKnots * 10).rounded() / 10]
    }
}
