import SwiftUI
import WatchKit

@main
struct WindsurferTrackerWatchApp: App {
    @WKApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var viewModel = WatchTrackerViewModel()

    var body: some Scene {
        WindowGroup {
            WatchContentView()
                .environmentObject(viewModel)
                .onAppear {
                    // Share viewModel with app delegate for action button handling
                    appDelegate.viewModel = viewModel
                    appDelegate.setupActionButton()
                }
        }
    }
}

// MARK: - App Delegate for Action Button

class AppDelegate: NSObject, WKApplicationDelegate {
    weak var viewModel: WatchTrackerViewModel?

    func setupActionButton() {
        // Note: Action button handling on Apple Watch Ultra requires watchOS 10+
        // and the app to be set as the action button target in Watch Settings.
        // The tap detection via accelerometer works as the primary trigger mechanism.
        // Action button support is a bonus for Ultra users who configure it.
        print("[ACTION] Tap detection is primary trigger; action button via Watch Settings")
    }

    // Handle action button launching the app
    func applicationDidBecomeActive() {
        // If launched from action button while race timer is enabled, we could start countdown
        // But we don't know if it was the action button or just opening the app
        print("[ACTION] App became active")
    }
}
