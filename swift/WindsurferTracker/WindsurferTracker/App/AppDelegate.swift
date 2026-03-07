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

        guard UIApplication.shared.isProtectedDataAvailable else {
            // Device not yet unlocked after reboot — UserDefaults is encrypted.
            // Accessing PreferencesManager now would load empty/default values,
            // overwriting real settings. Defer startup until data is available.
            NotificationCenter.default.addObserver(
                forName: UIApplication.protectedDataDidBecomeAvailableNotification,
                object: nil, queue: .main
            ) { [weak self] _ in
                self?.performStartup(backgroundLocationRelaunch: backgroundLocationRelaunch)
            }
            return true
        }

        performStartup(backgroundLocationRelaunch: backgroundLocationRelaunch)
        return true
    }

    private func performStartup(backgroundLocationRelaunch: Bool) {
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
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        // Tracking continues via background location mode
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        // Refresh UI state if needed
    }
}
