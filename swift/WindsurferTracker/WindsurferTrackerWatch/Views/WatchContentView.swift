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
        VStack(spacing: 8) {
            // Header with settings gear
            HStack {
                Spacer()
                NavigationLink {
                    WatchSettingsView()
                        .environmentObject(viewModel)
                } label: {
                    Image(systemName: "gearshape")
                        .font(.title3)
                        .foregroundColor(.gray)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 8)
            .padding(.top, 4)

            Spacer()

            // ID display
            VStack(spacing: 4) {
                Text("ID")
                    .font(.caption2)
                    .foregroundColor(.gray)
                HStack(spacing: 4) {
                    Text(viewModel.sailorId.isEmpty ? "Not Set" : viewModel.sailorId)
                        .font(.title2)
                        .bold()
                    if viewModel.highFrequencyMode {
                        Text("1Hz")
                            .font(.caption2)
                            .bold()
                            .foregroundColor(.cyan)
                    }
                }
            }

            // Server
            Text(viewModel.serverHost)
                .font(.caption2)
                .foregroundColor(.gray)

            // Error message
            if let error = viewModel.errorMessage {
                Text(error)
                    .font(.caption2)
                    .foregroundColor(.red)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 8)
            }

            Spacer()

            // Start button - styled like WearOS
            Button {
                viewModel.startTracking()
            } label: {
                HStack {
                    Image(systemName: "play.fill")
                    Text("Start")
                        .bold()
                }
                .font(.body)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .background(Color.green)
                .foregroundColor(.black)
                .cornerRadius(24)
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 16)
            .padding(.bottom, 8)
        }
    }
}

/// Watch settings view
struct WatchSettingsView: View {
    @EnvironmentObject var viewModel: WatchTrackerViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var tempId: String = ""
    @State private var tempHost: String = ""
    @State private var tempPassword: String = ""

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                Text("Settings")
                    .font(.headline)
                    .bold()

                // Your Name / ID
                VStack(alignment: .leading, spacing: 4) {
                    Text("Your Name")
                        .font(.caption)
                        .foregroundColor(.gray)
                    TextField("", text: $tempId)
                        .textFieldStyle(.plain)
                        .padding(8)
                        .background(Color.gray.opacity(0.3))
                        .cornerRadius(8)
                }

                // Role (tap to cycle)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Role")
                        .font(.caption)
                        .foregroundColor(.gray)
                    Button {
                        // Cycle through roles
                        switch viewModel.role {
                        case .sailor:
                            viewModel.role = .support
                        case .support:
                            viewModel.role = .spectator
                        case .spectator:
                            viewModel.role = .sailor
                        }
                    } label: {
                        HStack {
                            Text(viewModel.role.rawValue.capitalized)
                                .font(.caption)
                            Spacer()
                            Image(systemName: "chevron.right")
                                .font(.caption2)
                                .foregroundColor(.gray)
                        }
                        .padding(8)
                        .background(Color.gray.opacity(0.3))
                        .cornerRadius(8)
                    }
                    .buttonStyle(.plain)
                }

                // 1Hz mode
                Toggle(isOn: $viewModel.highFrequencyMode) {
                    VStack(alignment: .leading) {
                        Text("1Hz Mode")
                            .font(.caption)
                        Text("High frequency updates")
                            .font(.caption2)
                            .foregroundColor(.gray)
                    }
                }

                // Event selection
                VStack(alignment: .leading, spacing: 4) {
                    Text("Event")
                        .font(.caption)
                        .foregroundColor(.gray)
                    Button {
                        viewModel.cycleEvent()
                    } label: {
                        HStack {
                            if viewModel.eventsLoading {
                                ProgressView()
                                    .scaleEffect(0.7)
                            } else {
                                Text(viewModel.currentEventName)
                                    .font(.caption)
                            }
                            Spacer()
                            Image(systemName: "chevron.right")
                                .font(.caption2)
                                .foregroundColor(.gray)
                        }
                        .padding(8)
                        .background(Color.gray.opacity(0.3))
                        .cornerRadius(8)
                    }
                    .buttonStyle(.plain)
                }

                // Password
                VStack(alignment: .leading, spacing: 4) {
                    Text("Password")
                        .font(.caption)
                        .foregroundColor(.gray)
                    TextField("", text: $tempPassword)
                        .textFieldStyle(.plain)
                        .textInputAutocapitalization(.never)
                        .padding(8)
                        .background(Color.gray.opacity(0.3))
                        .cornerRadius(8)
                }

                // Server (at bottom like WearOS)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Server")
                        .font(.caption)
                        .foregroundColor(.gray)
                    TextField("wstracker.org", text: $tempHost)
                        .textFieldStyle(.plain)
                        .textInputAutocapitalization(.never)
                        .padding(8)
                        .background(Color.gray.opacity(0.3))
                        .cornerRadius(8)
                }

                // Save button
                Button {
                    viewModel.sailorId = tempId
                    viewModel.serverHost = tempHost
                    viewModel.password = tempPassword
                    dismiss()
                } label: {
                    Text("Save")
                        .font(.body)
                        .bold()
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(Color.blue)
                        .foregroundColor(.white)
                        .cornerRadius(20)
                }
                .buttonStyle(.plain)

                // Version string
                Text(versionString)
                    .font(.system(size: 10))
                    .foregroundColor(.gray)
                    .padding(.top, 8)
            }
            .padding(.horizontal, 8)
        }
        .onAppear {
            tempId = viewModel.sailorId
            tempHost = viewModel.serverHost
            tempPassword = viewModel.password
            viewModel.fetchEvents()
        }
    }

    private var versionString: String {
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        let gitHash = Bundle.main.infoDictionary?["GIT_HASH"] as? String

        if let hash = gitHash, !hash.isEmpty {
            return "\(version) (\(build)) \(hash)"
        } else {
            return "\(version) (\(build))"
        }
    }
}

#Preview {
    WatchContentView()
}
