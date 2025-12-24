import Foundation
import Network
import Combine

/// Network manager for UDP and HTTP communication with the tracker server
public actor NetworkManager {
    // MARK: - Publishers (nonisolated for Combine compatibility)

    /// Publisher for received ACK responses
    public nonisolated let ackPublisher = PassthroughSubject<AckResponse, Never>()

    /// Publisher for network errors
    public nonisolated let errorPublisher = PassthroughSubject<TrackerError, Never>()

    /// Publisher for connection state changes
    public nonisolated let connectionStatePublisher = CurrentValueSubject<Bool, Never>(false)

    // MARK: - State

    private var udpConnection: NWConnection?
    private var udpListener: NWConnection?
    private let dnsResolver = DNSResolver()
    private var useHttpFallback = false
    private var consecutiveUdpFailures = 0
    private var lastHttpRetryTime: Date?

    // ACK tracking
    private var pendingAcks: [Int: CheckedContinuation<AckResponse?, Never>] = [:]

    // MARK: - Configuration

    private var serverHost: String = TrackerConfig.defaultServerHost
    private var serverPort: UInt16 = TrackerConfig.defaultServerPort

    // MARK: - Initialization

    public init() {}

    // MARK: - Configuration

    /// Configure the server endpoint
    public func configure(host: String, port: UInt16) {
        serverHost = host
        serverPort = port
        // Reset connection state
        useHttpFallback = false
        consecutiveUdpFailures = 0
        closeUDPConnection()
    }

    // MARK: - Send Methods

    /// Send a tracker packet and wait for ACK
    /// - Returns: ACK response if received, nil on timeout
    public func send(_ packet: TrackerPacket) async -> AckResponse? {
        // Check if we should try UDP again
        if useHttpFallback {
            if let lastRetry = lastHttpRetryTime,
               Date().timeIntervalSince(lastRetry) >= TrackerConfig.httpRetryIntervalSeconds {
                // Try UDP again
                useHttpFallback = false
                consecutiveUdpFailures = 0
            }
        }

        guard let data = try? packet.toJSONData() else {
            errorPublisher.send(.encodingFailed)
            return nil
        }

        if useHttpFallback {
            return await sendHTTP(data, sequence: packet.sq)
        } else {
            return await sendUDP(data, sequence: packet.sq)
        }
    }

    // MARK: - UDP Communication

    private func sendUDP(_ data: Data, sequence: Int) async -> AckResponse? {
        // Resolve DNS
        let (host, _) = await dnsResolver.resolve(serverHost)
        let endpoint = NWEndpoint.hostPort(host: host, port: NWEndpoint.Port(rawValue: serverPort)!)
        print("[NET] UDP send seq=\(sequence) to \(serverHost):\(serverPort) -> \(host)")

        // Create or reuse connection
        if udpConnection == nil || udpConnection?.state == .cancelled {
            print("[NET] Creating new UDP connection...")
            await createUDPConnection(to: endpoint)
        }

        guard let connection = udpConnection else {
            print("[NET] UDP connection failed to create")
            await handleUDPFailure()
            return nil
        }

        print("[NET] UDP connection state: \(connection.state)")

        // Send with retries
        for attempt in 0..<TrackerConfig.udpRetryCount {
            // Send packet
            let sendSuccess = await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
                connection.send(content: data, completion: .contentProcessed { error in
                    if let error = error {
                        print("[NET] UDP send error: \(error)")
                    }
                    continuation.resume(returning: error == nil)
                })
            }

            guard sendSuccess else {
                print("[NET] UDP send failed attempt \(attempt + 1)")
                continue
            }

            print("[NET] UDP sent seq=\(sequence) attempt \(attempt + 1), waiting for ACK...")

            // Wait for ACK with timeout
            let ackResponse = await waitForACK(sequence: sequence, timeout: TrackerConfig.ackTimeoutSeconds)

            if let response = ackResponse {
                print("[NET] UDP ACK received for seq=\(response.ack)")
                consecutiveUdpFailures = 0
                nonisolated(unsafe) let pub = connectionStatePublisher
                pub.send(true)
                return response
            }

            print("[NET] UDP ACK timeout attempt \(attempt + 1)")

            // Delay before retry (except on last attempt)
            if attempt < TrackerConfig.udpRetryCount - 1 {
                try? await Task.sleep(nanoseconds: UInt64(TrackerConfig.udpRetryDelaySeconds * 1_000_000_000))
            }
        }

        // All retries failed
        print("[NET] UDP all \(TrackerConfig.udpRetryCount) attempts failed, failures=\(consecutiveUdpFailures + 1)")
        await handleUDPFailure()
        return nil
    }

    private func createUDPConnection(to endpoint: NWEndpoint) async {
        let parameters = NWParameters.udp
        parameters.allowLocalEndpointReuse = true

        let connection = NWConnection(to: endpoint, using: parameters)

        // Use a class to track if continuation was already resumed
        // (stateUpdateHandler can fire multiple times)
        final class ResumeTracker: @unchecked Sendable {
            var resumed = false
        }
        let tracker = ResumeTracker()

        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            connection.stateUpdateHandler = { state in
                print("[NET] UDP connection state changed: \(state)")
                switch state {
                case .ready, .failed, .cancelled:
                    if !tracker.resumed {
                        tracker.resumed = true
                        continuation.resume()
                    }
                default:
                    break
                }
            }
            connection.start(queue: .global(qos: .userInitiated))
        }

        udpConnection = connection
        print("[NET] UDP connection created, state: \(connection.state)")

        // Start receiving
        startReceiving(on: connection)
    }

    private func startReceiving(on connection: NWConnection) {
        connection.receiveMessage { [weak self] content, _, _, error in
            guard let self = self else { return }

            if let error = error {
                print("[NET] UDP receive error: \(error)")
            }

            if let data = content {
                print("[NET] UDP received \(data.count) bytes")
                Task {
                    await self.handleReceivedData(data)
                }
            }

            // Continue receiving if connection is still valid
            // For UDP, isComplete is true for each datagram, so we ignore it
            // and just check for errors and connection state
            if error == nil && connection.state == .ready {
                self.startReceiving(on: connection)
            }
        }
    }

    private func handleReceivedData(_ data: Data) async {
        guard let response = try? JSONDecoder().decode(AckResponse.self, from: data) else {
            return
        }

        // Notify any waiting continuations
        if let continuation = pendingAcks.removeValue(forKey: response.ack) {
            continuation.resume(returning: response)
        }

        // Also publish for general subscribers
        ackPublisher.send(response)
    }

    private func waitForACK(sequence: Int, timeout: TimeInterval) async -> AckResponse? {
        return await withTaskGroup(of: AckResponse?.self) { group in
            // Add ACK waiting task
            group.addTask {
                await withCheckedContinuation { continuation in
                    Task {
                        await self.registerPendingACK(sequence: sequence, continuation: continuation)
                    }
                }
            }

            // Add timeout task
            group.addTask {
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                return nil
            }

            // Return first result (either ACK or timeout)
            for await result in group {
                group.cancelAll()
                // Clean up pending ACK
                Task {
                    await self.removePendingACK(sequence: sequence)
                }
                return result
            }

            return nil
        }
    }

    private func registerPendingACK(sequence: Int, continuation: CheckedContinuation<AckResponse?, Never>) {
        pendingAcks[sequence] = continuation
    }

    private func removePendingACK(sequence: Int) {
        if let continuation = pendingAcks.removeValue(forKey: sequence) {
            continuation.resume(returning: nil)
        }
    }

    private func handleUDPFailure() async {
        consecutiveUdpFailures += 1

        if consecutiveUdpFailures >= TrackerConfig.httpFallbackThreshold {
            print("[NET] Switching to HTTP fallback after \(consecutiveUdpFailures) UDP failures")
            useHttpFallback = true
            lastHttpRetryTime = Date()
            nonisolated(unsafe) let pub = connectionStatePublisher
            pub.send(false)
        }
    }

    private func closeUDPConnection() {
        udpConnection?.cancel()
        udpConnection = nil
    }

    // MARK: - HTTP Fallback

    private func sendHTTP(_ data: Data, sequence: Int) async -> AckResponse? {
        // Determine protocol
        let proto = serverHost == "wstracker.org" || serverPort == 443 ? "https" : "http"
        let port = serverHost == "wstracker.org" ? 443 : serverPort

        guard let url = URL(string: "\(proto)://\(serverHost):\(port)/api/tracker") else {
            print("[NET] HTTP invalid URL")
            errorPublisher.send(.serverUnreachable)
            return nil
        }

        print("[NET] HTTP POST seq=\(sequence) to \(url)")

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = data
        request.timeoutInterval = TrackerConfig.ackTimeoutSeconds

        do {
            let (responseData, response) = try await URLSession.shared.data(for: request)

            guard let httpResponse = response as? HTTPURLResponse,
                  httpResponse.statusCode == 200 else {
                let status = (response as? HTTPURLResponse)?.statusCode ?? -1
                print("[NET] HTTP error status=\(status)")
                errorPublisher.send(.serverUnreachable)
                return nil
            }

            let ackResponse = try JSONDecoder().decode(AckResponse.self, from: responseData)
            print("[NET] HTTP ACK received for seq=\(ackResponse.ack)")
            ackPublisher.send(ackResponse)
            return ackResponse

        } catch {
            print("[NET] HTTP error: \(error)")
            errorPublisher.send(.serverUnreachable)
            return nil
        }
    }

    // MARK: - Event Fetching

    /// Fetch events list from server
    public func fetchEvents() async -> [EventInfo] {
        // Determine protocol
        let proto = serverHost == "wstracker.org" || serverPort == 443 ? "https" : "http"
        let port = serverHost == "wstracker.org" ? 443 : serverPort

        guard let url = URL(string: "\(proto)://\(serverHost):\(port)/api/events") else {
            return []
        }

        do {
            let (data, response) = try await URLSession.shared.data(from: url)

            guard let httpResponse = response as? HTTPURLResponse,
                  httpResponse.statusCode == 200 else {
                return []
            }

            let eventsResponse = try JSONDecoder().decode(EventsResponse.self, from: data)
            return eventsResponse.events

        } catch {
            return []
        }
    }

    // MARK: - Status

    /// Check if using HTTP fallback
    public var isUsingHttpFallback: Bool {
        useHttpFallback
    }

    /// Get consecutive UDP failure count
    public var udpFailureCount: Int {
        consecutiveUdpFailures
    }
}
