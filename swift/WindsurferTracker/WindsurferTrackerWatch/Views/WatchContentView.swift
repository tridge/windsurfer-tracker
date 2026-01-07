import SwiftUI
import WatchKit

/// Main watch view with compact interface
struct WatchContentView: View {
    @EnvironmentObject var viewModel: WatchTrackerViewModel
    @State private var navigateToSettings = false

    var body: some View {
        NavigationView {
            if viewModel.isTracking {
                WatchTrackingView()
                    .environmentObject(viewModel)
            } else {
                WatchConfigView(navigateToSettings: $navigateToSettings)
                    .environmentObject(viewModel)
            }
        }
        .onAppear {
            // Auto-navigate to settings if ID or password is missing
            if viewModel.needsSetup {
                navigateToSettings = true
            }
        }
    }
}

/// Pre-tracking config for watch
struct WatchConfigView: View {
    @EnvironmentObject var viewModel: WatchTrackerViewModel
    @Binding var navigateToSettings: Bool

    var body: some View {
        VStack(spacing: 8) {
            // Header with settings gear - left side to avoid clock
            HStack {
                NavigationLink(isActive: $navigateToSettings) {
                    WatchSettingsView(needsSetup: viewModel.needsSetup)
                        .environmentObject(viewModel)
                } label: {
                    Image(systemName: "gearshape")
                        .font(.body)
                        .foregroundColor(.gray)
                }
                .buttonStyle(.plain)
                Spacer()
            }
            .padding(.leading, 16)
            .padding(.top, 20)

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
    @State private var validationError: String? = nil
    @State private var isCheckingPassword = false

    // Track original auth values to detect changes
    @State private var originalSailorId: String = ""
    @State private var originalPassword: String = ""
    @State private var originalEventId: Int = 0

    /// Whether this was opened because setup is required (prevents dismissing without valid settings)
    var needsSetup: Bool = false

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

                // Heart rate
                Toggle(isOn: $viewModel.heartRateEnabled) {
                    VStack(alignment: .leading) {
                        Text("Heart Rate")
                            .font(.caption)
                        Text("Send heart rate data")
                            .font(.caption2)
                            .foregroundColor(.gray)
                    }
                }

                // Tracking buzz
                Toggle(isOn: $viewModel.trackerBeep) {
                    VStack(alignment: .leading) {
                        Text("Tracking Buzz")
                            .font(.caption)
                        Text("Reminder buzz each minute")
                            .font(.caption2)
                            .foregroundColor(.gray)
                    }
                }

                // Race Timer
                Toggle(isOn: $viewModel.raceTimerEnabled) {
                    VStack(alignment: .leading) {
                        Text("Race Timer")
                            .font(.caption)
                        Text("Countdown with voice")
                            .font(.caption2)
                            .foregroundColor(.gray)
                    }
                }

                // Timer minutes (only show if race timer enabled)
                if viewModel.raceTimerEnabled {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Countdown Minutes")
                            .font(.caption)
                            .foregroundColor(.gray)
                        HStack {
                            Button {
                                if viewModel.raceTimerMinutes > 1 {
                                    viewModel.raceTimerMinutes -= 1
                                }
                            } label: {
                                Image(systemName: "minus.circle.fill")
                                    .font(.title3)
                                    .foregroundColor(viewModel.raceTimerMinutes > 1 ? .blue : .gray)
                            }
                            .buttonStyle(.plain)
                            .disabled(viewModel.raceTimerMinutes <= 1)

                            Text("\(viewModel.raceTimerMinutes)")
                                .font(.title2)
                                .bold()
                                .frame(minWidth: 40)

                            Button {
                                if viewModel.raceTimerMinutes < 9 {
                                    viewModel.raceTimerMinutes += 1
                                }
                            } label: {
                                Image(systemName: "plus.circle.fill")
                                    .font(.title3)
                                    .foregroundColor(viewModel.raceTimerMinutes < 9 ? .blue : .gray)
                            }
                            .buttonStyle(.plain)
                            .disabled(viewModel.raceTimerMinutes >= 9)
                        }
                        .frame(maxWidth: .infinity)
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

                // Validation error
                if let error = validationError {
                    Text(error)
                        .font(.caption2)
                        .foregroundColor(.red)
                        .multilineTextAlignment(.center)
                }

                // Save button
                Button {
                    // Validate required fields
                    if tempId.isEmpty && tempPassword.isEmpty {
                        validationError = "Name and password required"
                        return
                    }
                    if tempId.isEmpty {
                        validationError = "Name is required"
                        return
                    }
                    if tempPassword.isEmpty {
                        validationError = "Password is required"
                        return
                    }

                    // Check if auth fields changed
                    let authFieldsChanged = tempId != originalSailorId ||
                        tempPassword != originalPassword ||
                        viewModel.eventId != originalEventId

                    if authFieldsChanged {
                        // Check password with server
                        isCheckingPassword = true
                        validationError = "Checking..."
                        Task { @MainActor in
                            let networkManager = NetworkManager()
                            await networkManager.configure(
                                host: tempHost,
                                port: UInt16(TrackerConfig.defaultServerPort)
                            )
                            let osVersion = "watchOS \(WKInterfaceDevice.current().systemVersion)"
                            let result = await networkManager.checkPassword(
                                eventId: viewModel.eventId,
                                password: tempPassword,
                                userId: tempId,
                                userOs: osVersion,
                                userVer: versionString
                            )

                            isCheckingPassword = false

                            switch result {
                            case .success:
                                validationError = nil
                                viewModel.sailorId = tempId
                                viewModel.serverHost = tempHost
                                viewModel.password = tempPassword
                                dismiss()
                            case .failure(let error):
                                validationError = error.localizedDescription
                            }
                        }
                    } else {
                        // No auth fields changed, save directly
                        validationError = nil
                        viewModel.sailorId = tempId
                        viewModel.serverHost = tempHost
                        viewModel.password = tempPassword
                        dismiss()
                    }
                } label: {
                    Text(isCheckingPassword ? "..." : "Save")
                        .font(.body)
                        .bold()
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(isCheckingPassword ? Color.gray : Color.blue)
                        .foregroundColor(.white)
                        .cornerRadius(20)
                }
                .buttonStyle(.plain)
                .disabled(isCheckingPassword)

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
            // Track original auth values
            originalSailorId = viewModel.sailorId
            originalPassword = viewModel.password
            originalEventId = viewModel.eventId
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
        .environmentObject(WatchTrackerViewModel())
}
