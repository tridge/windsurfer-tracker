import SwiftUI

/// Main container view switching between config and tracking modes
struct ContentView: View {
    @EnvironmentObject var viewModel: TrackerViewModel
    @ObservedObject private var preferences = PreferencesManager.shared

    var body: some View {
        ZStack {
            Color.white.ignoresSafeArea()

            if !preferences.eulaAccepted {
                EULAView(eulaAccepted: $preferences.eulaAccepted)
            } else {
                Group {
                    if viewModel.isTracking {
                        TrackingView()
                    } else {
                        ConfigView()
                    }
                }
            }
        }
        .sheet(isPresented: $viewModel.showSettings) {
            SettingsView()
                .environmentObject(viewModel)
        }
        .alert("Error", isPresented: $viewModel.showError) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(viewModel.errorMessage ?? "Unknown error")
        }
    }
}

#Preview {
    ContentView()
        .environmentObject(TrackerViewModel())
}
