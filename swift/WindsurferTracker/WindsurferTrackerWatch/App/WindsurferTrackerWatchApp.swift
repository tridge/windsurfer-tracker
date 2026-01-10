import SwiftUI
import WatchKit
import AppIntents

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
                }
                .task {
                    // Register app shortcuts for Action button
                    if #available(watchOS 10.0, *) {
                        RaceTimerShortcutsProvider.updateAppShortcutParameters()
                    }
                }
                .onContinueUserActivity("com.apple.watchkit.action-button") { _ in
                    // Handle Action button press
                    print("[ACTION] Action button via onContinueUserActivity")
                    viewModel.handleActionButton()
                }
        }
    }
}

// MARK: - App Delegate for Action Button

class AppDelegate: NSObject, WKApplicationDelegate {
    weak var viewModel: WatchTrackerViewModel?

    func applicationDidBecomeActive() {
        print("[ACTION] App became active")
    }

    // Handle Action button press when app is in foreground
    func handleUserActivity(_ userActivity: NSUserActivity) {
        print("[ACTION] handleUserActivity: \(userActivity.activityType)")
        if userActivity.activityType == "com.apple.watchkit.action-button" {
            print("[ACTION] Action button pressed!")
            Task { @MainActor in
                viewModel?.handleActionButton()
            }
        }
    }
}
