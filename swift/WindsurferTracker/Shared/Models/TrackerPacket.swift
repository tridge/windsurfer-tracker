import Foundation

/// Outgoing position packet sent to server via UDP or HTTP
public struct TrackerPacket: Codable {
    /// Sailor/tracker ID (e.g., "S07")
    public let id: String

    /// Event ID
    public let eid: Int

    /// Sequence number for ACK tracking
    public let sq: Int

    /// Unix timestamp (seconds)
    public let ts: Int

    /// Latitude (nil if using pos array in 1Hz mode)
    public let lat: Double?

    /// Longitude (nil if using pos array in 1Hz mode)
    public let lon: Double?

    /// Speed in knots
    public let spd: Double

    /// Heading 0-360 degrees
    public let hdg: Int

    /// Assist requested flag
    public let ast: Bool

    /// Battery percentage (0-100)
    public let bat: Int

    /// Signal strength (-1 for iOS as not available)
    public let sig: Int

    /// Role: sailor, support, spectator
    public let role: String

    /// App version string
    public let ver: String

    /// OS version string (e.g., "iOS 17.2")
    public let os: String

    /// Password (optional, omitted if empty)
    public let pwd: String?

    /// Battery drain rate in %/hour (optional, calculated after 5 min)
    public let bdr: Double?

    /// Charging state
    public let chg: Bool

    /// Power save / low power mode
    public let ps: Bool

    /// Position array for 1Hz mode: [[ts, lat, lon], ...]
    public let pos: [[Double]]?

    /// Horizontal accuracy in meters (optional)
    public let hac: Double?

    /// Heart rate in BPM (optional, only if enabled and available)
    public let hr: Int?

    /// Stopped flag - true when user deliberately stops tracking
    public let stopped: Bool?

    public init(
        id: String,
        eid: Int,
        sq: Int,
        ts: Int,
        lat: Double?,
        lon: Double?,
        spd: Double,
        hdg: Int,
        ast: Bool,
        bat: Int,
        sig: Int = TrackerConfig.signalStrengthUnavailable,
        role: String,
        ver: String,
        os: String,
        pwd: String? = nil,
        bdr: Double? = nil,
        chg: Bool,
        ps: Bool,
        pos: [[Double]]? = nil,
        hac: Double? = nil,
        hr: Int? = nil,
        stopped: Bool? = nil
    ) {
        self.id = id
        self.eid = eid
        self.sq = sq
        self.ts = ts
        self.lat = lat
        self.lon = lon
        self.spd = spd
        self.hdg = hdg
        self.ast = ast
        self.bat = bat
        self.sig = sig
        self.role = role
        self.ver = ver
        self.os = os
        self.pwd = pwd?.isEmpty == true ? nil : pwd
        self.bdr = bdr
        self.chg = chg
        self.ps = ps
        self.pos = pos
        self.hac = hac
        self.hr = hr
        self.stopped = stopped
    }

    /// Encode to JSON data
    public func toJSONData() throws -> Data {
        let encoder = JSONEncoder()
        return try encoder.encode(self)
    }
}

/// ACK response from server
public struct AckResponse: Codable {
    /// Acknowledged sequence number
    public let ack: Int

    /// Server timestamp
    public let ts: Int

    /// Event name (optional)
    public let event: String?

    /// Error type (optional, e.g., "auth")
    public let error: String?

    /// Error message (optional)
    public let msg: String?

    public var isSuccess: Bool {
        return error == nil
    }

    public var isAuthError: Bool {
        return error == "auth"
    }
}

/// Event info from /api/events endpoint
public struct EventInfo: Codable, Identifiable {
    public let eid: Int
    public let name: String
    public let description: String?

    public var id: Int { eid }

    public init(eid: Int, name: String, description: String? = nil) {
        self.eid = eid
        self.name = name
        self.description = description
    }
}

/// Response from /api/events
public struct EventsResponse: Codable {
    public let events: [EventInfo]
}
