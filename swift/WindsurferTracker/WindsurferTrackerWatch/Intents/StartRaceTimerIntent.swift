import Foundation
import AppIntents
import WatchKit

/// App Intent for starting the race countdown timer from the Action button
@available(watchOS 10.0, *)
struct StartRaceTimerIntent: AppIntent {
    static var title: LocalizedStringResource = "Start Race Timer"
    static var description: IntentDescription = IntentDescription("Starts the race countdown timer")

    static var openAppWhenRun: Bool = true

    @MainActor
    func perform() async throws -> some IntentResult {
        // Get the shared view model and start the countdown
        if let viewModel = await getViewModel() {
            // Only start if tracking is active and race timer is enabled
            if viewModel.isTracking && viewModel.raceTimerEnabled {
                // If already running, reset and restart
                if viewModel.countdownSeconds != nil {
                    viewModel.resetCountdown()
                    // Small delay to ensure reset completes
                    try? await Task.sleep(nanoseconds: 100_000_000)  // 0.1s
                }

                // Start the countdown
                viewModel.startCountdown()

                return .result(dialog: "Race timer started")
            } else if !viewModel.isTracking {
                return .result(dialog: "Start tracking first")
            } else {
                return .result(dialog: "Enable race timer in settings")
            }
        }

        return .result(dialog: "Unable to start race timer")
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
