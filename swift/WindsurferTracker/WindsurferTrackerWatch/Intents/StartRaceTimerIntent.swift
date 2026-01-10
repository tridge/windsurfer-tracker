import Foundation
import AppIntents
import WatchKit

/// App Intent for race timer control from the Action button
@available(watchOS 10.0, *)
struct StartRaceTimerIntent: AppIntent {
    static var title: LocalizedStringResource = "Race Timer"
    static var description: IntentDescription = IntentDescription("Start/reset the race countdown timer")

    static var openAppWhenRun: Bool = true

    @MainActor
    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        // Get the shared view model
        if let viewModel = await getViewModel() {
            // Only works if tracking is active and race timer is enabled
            if viewModel.isTracking && viewModel.raceTimerEnabled {
                // Use the same state machine as tap detection
                viewModel.handleActionButton()
                return .result(value: "OK")
            } else if !viewModel.isTracking {
                return .result(value: "Start tracking first")
            } else {
                return .result(value: "Enable race timer")
            }
        }

        return .result(value: "Error")
    }

    @MainActor
    private func getViewModel() async -> WatchTrackerViewModel? {
        // Access the shared view model from the app delegate
        guard let delegate = WKApplication.shared().delegate as? AppDelegate else {
            return nil
        }
        return delegate.viewModel
    }
}

/// Configuration for Action button shortcuts
@available(watchOS 10.0, *)
struct RaceTimerShortcutsProvider: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: StartRaceTimerIntent(),
            phrases: [
                "Start race timer in \(.applicationName)",
                "Begin countdown in \(.applicationName)"
            ],
            shortTitle: "Start Race",
            systemImageName: "stopwatch"
        )
    }
}
