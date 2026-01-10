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
        }
    }
}

// MARK: - App Delegate for Action Button

class AppDelegate: NSObject, WKApplicationDelegate {
    weak var viewModel: WatchTrackerViewModel?

    func applicationDidBecomeActive() {
        print("[ACTION] App became active")
    }
}
