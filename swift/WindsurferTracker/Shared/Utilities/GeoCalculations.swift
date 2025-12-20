import Foundation
import CoreLocation

/// Geographic calculation utilities
public enum GeoCalculations {
    /// Conversion factor from meters per second to knots
    public static let metersPerSecondToKnotsMultiplier: Double = 1.94384

    /// Convert speed from m/s to knots
    public static func metersPerSecondToKnots(_ mps: Double) -> Double {
        mps * metersPerSecondToKnotsMultiplier
    }

    /// Convert speed from knots to m/s
    public static func knotsToMetersPerSecond(_ knots: Double) -> Double {
        knots / metersPerSecondToKnotsMultiplier
    }

    /// Earth radius in meters
    private static let earthRadiusMeters: Double = 6_371_000

    /// Calculate distance between two coordinates using Haversine formula
    /// - Returns: Distance in meters
    public static func distance(
        from: CLLocationCoordinate2D,
        to: CLLocationCoordinate2D
    ) -> Double {
        let lat1 = from.latitude * .pi / 180
        let lat2 = to.latitude * .pi / 180
        let dLat = (to.latitude - from.latitude) * .pi / 180
        let dLon = (to.longitude - from.longitude) * .pi / 180

        let a = sin(dLat / 2) * sin(dLat / 2) +
                cos(lat1) * cos(lat2) * sin(dLon / 2) * sin(dLon / 2)
        let c = 2 * atan2(sqrt(a), sqrt(1 - a))

        return earthRadiusMeters * c
    }

    /// Calculate bearing from one coordinate to another
    /// - Returns: Bearing in degrees (0-360)
    public static func bearing(
        from: CLLocationCoordinate2D,
        to: CLLocationCoordinate2D
    ) -> Double {
        let lat1 = from.latitude * .pi / 180
        let lat2 = to.latitude * .pi / 180
        let dLon = (to.longitude - from.longitude) * .pi / 180

        let y = sin(dLon) * cos(lat2)
        let x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dLon)

        var bearing = atan2(y, x) * 180 / .pi
        if bearing < 0 {
            bearing += 360
        }
        return bearing
    }

    /// Calculate speed from two positions and time difference
    /// - Returns: Speed in knots
    public static func speed(
        from: CLLocationCoordinate2D,
        to: CLLocationCoordinate2D,
        timeSeconds: TimeInterval
    ) -> Double {
        guard timeSeconds > 0 else { return 0 }
        let distanceMeters = distance(from: from, to: to)
        let speedMps = distanceMeters / timeSeconds
        return metersPerSecondToKnots(speedMps)
    }

    /// Normalize heading to 0-360 range
    public static func normalizeHeading(_ heading: Double) -> Int {
        var h = heading.truncatingRemainder(dividingBy: 360)
        if h < 0 { h += 360 }
        return Int(h.rounded())
    }
}
