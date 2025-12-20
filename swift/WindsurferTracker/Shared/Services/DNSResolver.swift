import Foundation
import Network

/// DNS resolver with caching for reliable server lookups
public actor DNSResolver {
    /// Cached resolution result
    private struct CachedResolution {
        let host: NWEndpoint.Host
        let timestamp: Date
        let isIPv6: Bool
    }

    /// Cache of resolved hosts
    private var cache: [String: CachedResolution] = [:]

    /// Cache TTL (5 minutes)
    private let cacheTTL: TimeInterval = TrackerConfig.dnsRefreshInterval

    public init() {}

    /// Resolve hostname to NWEndpoint.Host with caching
    /// - Parameter hostname: The hostname to resolve
    /// - Returns: Resolved host (IP address or original hostname if resolution fails)
    public func resolve(_ hostname: String) async -> (host: NWEndpoint.Host, isIPv6: Bool) {
        // Check if it's already an IP address
        if isIPAddress(hostname) {
            let isIPv6 = hostname.contains(":")
            return (NWEndpoint.Host(hostname), isIPv6)
        }

        // Check cache
        if let cached = cache[hostname],
           Date().timeIntervalSince(cached.timestamp) < cacheTTL {
            return (cached.host, cached.isIPv6)
        }

        // Perform DNS lookup
        do {
            let (resolvedIP, isIPv6) = try await performDNSLookup(hostname)
            let host = NWEndpoint.Host(resolvedIP)

            // Cache the result
            cache[hostname] = CachedResolution(
                host: host,
                timestamp: Date(),
                isIPv6: isIPv6
            )

            return (host, isIPv6)
        } catch {
            // On failure, use cached value if available (even if expired)
            if let cached = cache[hostname] {
                return (cached.host, cached.isIPv6)
            }

            // Fall back to using hostname directly
            return (NWEndpoint.Host(hostname), false)
        }
    }

    /// Clear the DNS cache
    public func clearCache() {
        cache.removeAll()
    }

    /// Remove expired entries from cache
    public func pruneExpiredEntries() {
        let now = Date()
        cache = cache.filter { _, value in
            now.timeIntervalSince(value.timestamp) < cacheTTL * 2  // Keep for 2x TTL as fallback
        }
    }

    // MARK: - Private Methods

    private func isIPAddress(_ string: String) -> Bool {
        // Check for IPv4
        var sin = sockaddr_in()
        if inet_pton(AF_INET, string, &sin.sin_addr) == 1 {
            return true
        }

        // Check for IPv6
        var sin6 = sockaddr_in6()
        if inet_pton(AF_INET6, string, &sin6.sin6_addr) == 1 {
            return true
        }

        return false
    }

    private func performDNSLookup(_ hostname: String) async throws -> (ip: String, isIPv6: Bool) {
        return try await withCheckedThrowingContinuation { continuation in
            var hints = addrinfo()
            hints.ai_family = AF_UNSPEC  // Allow both IPv4 and IPv6
            hints.ai_socktype = SOCK_DGRAM

            var result: UnsafeMutablePointer<addrinfo>?

            let status = getaddrinfo(hostname, nil, &hints, &result)
            guard status == 0, let addrInfo = result else {
                continuation.resume(throwing: DNSError.resolutionFailed)
                return
            }

            defer { freeaddrinfo(result) }

            // Prefer IPv4, but use IPv6 if that's all we have
            var ipv4Address: String?
            var ipv6Address: String?

            var current: UnsafeMutablePointer<addrinfo>? = addrInfo
            while let info = current {
                if info.pointee.ai_family == AF_INET {
                    // IPv4
                    var addr = sockaddr_in()
                    memcpy(&addr, info.pointee.ai_addr, MemoryLayout<sockaddr_in>.size)
                    var buffer = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                    inet_ntop(AF_INET, &addr.sin_addr, &buffer, socklen_t(INET_ADDRSTRLEN))
                    ipv4Address = String(cString: buffer)
                } else if info.pointee.ai_family == AF_INET6 {
                    // IPv6
                    var addr = sockaddr_in6()
                    memcpy(&addr, info.pointee.ai_addr, MemoryLayout<sockaddr_in6>.size)
                    var buffer = [CChar](repeating: 0, count: Int(INET6_ADDRSTRLEN))
                    inet_ntop(AF_INET6, &addr.sin6_addr, &buffer, socklen_t(INET6_ADDRSTRLEN))
                    ipv6Address = String(cString: buffer)
                }
                current = info.pointee.ai_next
            }

            // Prefer IPv4
            if let ipv4 = ipv4Address {
                continuation.resume(returning: (ipv4, false))
            } else if let ipv6 = ipv6Address {
                continuation.resume(returning: (ipv6, true))
            } else {
                continuation.resume(throwing: DNSError.noAddressFound)
            }
        }
    }
}

/// DNS resolution errors
public enum DNSError: Error {
    case resolutionFailed
    case noAddressFound
}
