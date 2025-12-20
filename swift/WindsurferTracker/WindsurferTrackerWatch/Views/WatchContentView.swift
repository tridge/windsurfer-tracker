import SwiftUI

/// Main watch view with compact interface
struct WatchContentView: View {
    @StateObject private var viewModel = WatchTrackerViewModel()

    var body: some View {
        NavigationView {
            if viewModel.isTracking {
                WatchTrackingView()
                    .environmentObject(viewModel)
            } else {
                WatchConfigView()
                    .environmentObject(viewModel)
            }
        }
    }
}

/// Pre-tracking config for watch
struct WatchConfigView: View {
    @EnvironmentObject var viewModel: WatchTrackerViewModel

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                // ID display
                VStack(spacing: 4) {
                    Text("ID")
                        .font(.caption2)
                        .foregroundColor(.gray)
                    Text(viewModel.sailorId.isEmpty ? "Not Set" : viewModel.sailorId)
                        .font(.title3)
                        .fontWeight(.bold)
                }
                .padding(.top, 8)

                // Server status
                Text(viewModel.serverHost)
                    .font(.caption2)
                    .foregroundColor(.gray)

                Spacer()
                    .frame(height: 16)

                // Start button
                Button {
                    viewModel.startTracking()
                } label: {
                    HStack {
                        Image(systemName: "play.fill")
                        Text("Start")
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)

                // Settings link
                NavigationLink {
                    WatchSettingsView()
                        .environmentObject(viewModel)
                } label: {
                    HStack {
                        Image(systemName: "gear")
                        Text("Settings")
                    }
                    .font(.caption)
                }
                .buttonStyle(.bordered)
            }
            .padding(.horizontal, 8)
        }
        .navigationTitle("Tracker")
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// Watch settings view
struct WatchSettingsView: View {
    @EnvironmentObject var viewModel: WatchTrackerViewModel
    @State private var tempId: String = ""
    @State private var tempHost: String = ""

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                // Sailor ID
                VStack(alignment: .leading, spacing: 4) {
                    Text("Your ID")
                        .font(.caption2)
                        .foregroundColor(.gray)
                    TextField("W01", text: $tempId)
                }

                // Server
                VStack(alignment: .leading, spacing: 4) {
                    Text("Server")
                        .font(.caption2)
                        .foregroundColor(.gray)
                    TextField("wstracker.org", text: $tempHost)
                        .textInputAutocapitalization(.never)
                }

                // Role
                Picker("Role", selection: $viewModel.role) {
                    Text("Sailor").tag(TrackerRole.sailor)
                    Text("Support").tag(TrackerRole.support)
                    Text("Spectator").tag(TrackerRole.spectator)
                }

                // 1Hz mode
                Toggle("1Hz Mode", isOn: $viewModel.highFrequencyMode)
            }
            .padding(.horizontal, 8)
        }
        .navigationTitle("Settings")
        .onAppear {
            tempId = viewModel.sailorId
            tempHost = viewModel.serverHost
        }
        .onDisappear {
            viewModel.sailorId = tempId
            viewModel.serverHost = tempHost
        }
    }
}

#Preview {
    WatchContentView()
}
