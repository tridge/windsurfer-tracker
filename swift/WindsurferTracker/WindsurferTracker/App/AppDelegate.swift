import UIKit
import CoreLocation

class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Enable battery monitoring for accurate drain tracking
        UIDevice.current.isBatteryMonitoringEnabled = true

        // Check if launched due to location update
        if launchOptions?[.location] != nil {
            // App was launched in background due to location update
            // TrackerService will handle auto-resume
        }

        // Auto-resume tracking if it was active
        if PreferencesManager.shared.trackingActive {
            Task {
                do {
                    try await TrackerService.shared.start()
                } catch {
                    // Failed to auto-resume, reset state
                    PreferencesManager.shared.trackingActive = false
                }
            }
        }

        return true
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        // Tracking continues via background location mode
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        // Refresh UI state if needed
    }
}
