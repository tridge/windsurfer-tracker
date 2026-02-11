import UIKit
import CoreLocation

class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Enable battery monitoring for accurate drain tracking
        UIDevice.current.isBatteryMonitoringEnabled = true

        // Only auto-resume tracking if the OS relaunched us in the background
        // for a location event (keeps tracking alive across OS-initiated restarts).
        // For user-initiated launches (reboot, force-quit, tap icon), start in idle.
        let backgroundLocationRelaunch = launchOptions?[.location] != nil

        if backgroundLocationRelaunch && PreferencesManager.shared.trackingActive {
            Task {
                do {
                    try await TrackerService.shared.start()
                } catch {
                    PreferencesManager.shared.trackingActive = false
                }
            }
        } else if !PreferencesManager.shared.sailorId.isEmpty &&
                  !PreferencesManager.shared.password.isEmpty {
            // Clear stale tracking state and start in idle mode
            PreferencesManager.shared.trackingActive = false
            Task {
                await TrackerService.shared.startInIdleMode()
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
