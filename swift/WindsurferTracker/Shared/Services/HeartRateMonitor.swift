import Foundation
import Combine
#if os(watchOS)
import HealthKit
#endif

/// Monitor heart rate on watchOS using HealthKit
public final class HeartRateMonitor: ObservableObject {
    public static let shared = HeartRateMonitor()

    // MARK: - Published State

    @Published public private(set) var currentHeartRate: Int?
    @Published public private(set) var isMonitoring: Bool = false

    // MARK: - Private

    #if os(watchOS)
    private let healthStore = HKHealthStore()
    private var heartRateQuery: HKAnchoredObjectQuery?
    #endif

    private init() {}

    // MARK: - Public Methods

    /// Request HealthKit authorization for heart rate
    public func requestAuthorization() async -> Bool {
        #if os(watchOS)
        #if targetEnvironment(simulator)
        // Simulator: always return true for testing
        return true
        #else
        guard HKHealthStore.isHealthDataAvailable() else {
            return false
        }

        let heartRateType = HKQuantityType.quantityType(forIdentifier: .heartRate)!

        do {
            try await healthStore.requestAuthorization(toShare: [], read: [heartRateType])
            return true
        } catch {
            print("HeartRateMonitor: Authorization failed: \(error)")
            return false
        }
        #endif
        #else
        return false
        #endif
    }

    /// Start monitoring heart rate
    public func startMonitoring() {
        #if os(watchOS)
        guard !isMonitoring else { return }

        #if targetEnvironment(simulator)
        // Simulator: generate fake heart rate for testing
        DispatchQueue.main.async {
            self.isMonitoring = true
            self.currentHeartRate = Int.random(in: 70...90)
        }
        // Update periodically
        Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.currentHeartRate = Int.random(in: 70...90)
        }
        return
        #endif

        let heartRateType = HKQuantityType.quantityType(forIdentifier: .heartRate)!

        // Query for heart rate samples starting now
        let predicate = HKQuery.predicateForSamples(
            withStart: Date(),
            end: nil,
            options: .strictStartDate
        )

        heartRateQuery = HKAnchoredObjectQuery(
            type: heartRateType,
            predicate: predicate,
            anchor: nil,
            limit: HKObjectQueryNoLimit
        ) { [weak self] query, samples, deletedObjects, anchor, error in
            self?.processHeartRateSamples(samples)
        }

        heartRateQuery?.updateHandler = { [weak self] query, samples, deletedObjects, anchor, error in
            self?.processHeartRateSamples(samples)
        }

        if let query = heartRateQuery {
            healthStore.execute(query)
            DispatchQueue.main.async {
                self.isMonitoring = true
            }
        }
        #endif
    }

    /// Stop monitoring heart rate
    public func stopMonitoring() {
        #if os(watchOS)
        if let query = heartRateQuery {
            healthStore.stop(query)
            heartRateQuery = nil
        }
        DispatchQueue.main.async {
            self.isMonitoring = false
            self.currentHeartRate = nil
        }
        #endif
    }

    // MARK: - Private Methods

    #if os(watchOS)
    private func processHeartRateSamples(_ samples: [HKSample]?) {
        guard let samples = samples as? [HKQuantitySample], !samples.isEmpty else {
            return
        }

        // Get the most recent heart rate
        let latestSample = samples.sorted { $0.endDate > $1.endDate }.first
        if let sample = latestSample {
            let heartRateUnit = HKUnit.count().unitDivided(by: .minute())
            let heartRate = Int(sample.quantity.doubleValue(for: heartRateUnit))

            DispatchQueue.main.async {
                self.currentHeartRate = heartRate
            }
        }
    }
    #endif
}
