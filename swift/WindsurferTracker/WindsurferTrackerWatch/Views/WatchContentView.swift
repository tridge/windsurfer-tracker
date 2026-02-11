import SwiftUI
import WatchKit

// Feature flag: Race timer is disabled for this release due to audio issues
// Set to true to re-enable race timer functionality
let RACE_TIMER_FEATURE_ENABLED = false

/// Main watch view with compact interface
struct WatchContentView: View {
    @EnvironmentObject var viewModel: WatchTrackerViewModel
    @ObservedObject private var preferences = PreferencesManager.shared
    @State private var navigateToSettings = false

    var body: some View {
        if !preferences.eulaAccepted {
            WatchEULAView(eulaAccepted: $preferences.eulaAccepted)
        } else {
            NavigationView {
                if viewModel.isTracking || viewModel.isIdleMode {
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
}

/// Pre-tracking config for watch - matches tracking layout
struct WatchConfigView: View {
    @EnvironmentObject var viewModel: WatchTrackerViewModel
    @Binding var navigateToSettings: Bool

    var body: some View {
        ZStack {
            VStack(spacing: 4) {
                // Header with settings gear - top left to avoid clock
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
                .padding(.leading, 24)
                .padding(.top, 16)

                // Status: RED "STOPPED"
                Text("STOPPED")
                    .font(.caption)
                    .bold()
                    .foregroundColor(.red)

                // Event name
                Text(viewModel.currentEventName.isEmpty ? "---" : viewModel.currentEventName)
                    .font(.caption2)
                    .foregroundColor(.blue)
                    .lineLimit(1)

                // Sailor ID
                Text(viewModel.sailorId.isEmpty ? "Not Set" : viewModel.sailorId)
                    .font(.caption2)
                    .foregroundColor(.white)

                // Speed (always 0 when not tracking)
                HStack(alignment: .lastTextBaseline, spacing: 2) {
                    Text("0.0")
                        .font(.system(size: 36, weight: .bold, design: .rounded))
                        .foregroundColor(.white)
                    Text("kts")
                        .font(.caption)
                        .foregroundColor(.gray)
                }

                // Heart rate + Distance + Battery
                HStack(spacing: 12) {
                    // Heart rate (if enabled, show 0)
                    if viewModel.heartRateEnabled {
                        HStack(spacing: 2) {
                            Image(systemName: "heart.fill")
                                .font(.system(size: 10))
                                .foregroundColor(.red)
                            Text("0")
                                .font(.system(size: 14, weight: .semibold))
                                .foregroundColor(.white)
                        }
                    }

                    // Distance (always 0)
                    HStack(spacing: 2) {
                        Image(systemName: "arrow.triangle.swap")
                            .font(.system(size: 10))
                            .foregroundColor(.cyan)
                        Text("0m")
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundColor(.white)
                    }

                    // Battery percentage
                    HStack(spacing: 2) {
                        Image(systemName: batteryIconName)
                            .font(.system(size: 10))
                            .foregroundColor(batteryColor)
                        Text(batteryText)
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundColor(.white)
                    }
                }

                Spacer()

                // Start button
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

    // MARK: - Battery Display

    private var batteryLevel: Float {
        let device = WKInterfaceDevice.current()
        device.isBatteryMonitoringEnabled = true
        return device.batteryLevel
    }

    private var batteryText: String {
        let level = batteryLevel
        if level < 0 {
            return "--%"
        }
        return "\(Int(level * 100))%"
    }

    private var batteryIconName: String {
        let level = batteryLevel
        if level < 0 {
            return "battery.0"
        } else if level < 0.15 {
            return "battery.0"
        } else if level < 0.40 {
            return "battery.25"
        } else if level < 0.65 {
            return "battery.50"
        } else if level < 0.90 {
            return "battery.75"
        } else {
            return "battery.100"
        }
    }

    private var batteryColor: Color {
        let level = batteryLevel
        if level < 0 {
            return .gray
        } else if level < 0.20 {
            return .red
        } else if level < 0.40 {
            return .yellow
        } else {
            return .green
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
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .padding(8)
                        .background(Color.gray.opacity(0.3))
                        .cornerRadius(8)
                }

                // Password (visible, not SecureField)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Password")
                        .font(.caption)
                        .foregroundColor(.gray)
                    TextField("", text: $tempPassword)
                        .textFieldStyle(.plain)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .padding(8)
                        .background(Color.gray.opacity(0.3))
                        .cornerRadius(8)
                }

                // Event selection
                VStack(alignment: .leading, spacing: 4) {
                    Text("Event")
                        .font(.caption)
                        .foregroundColor(.gray)
                    Button {
                        viewModel.cycleEvent()
                        // Update tempPassword with newly loaded password
                        tempPassword = viewModel.password
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

                // Water Lock
                Toggle(isOn: $viewModel.waterLock) {
                    VStack(alignment: .leading) {
                        Text("Water Lock")
                            .font(.caption)
                        Text("Lock screen when tracking")
                            .font(.caption2)
                            .foregroundColor(.gray)
                    }
                }

                // Race Timer (disabled for this release)
                if RACE_TIMER_FEATURE_ENABLED {
                    Toggle(isOn: $viewModel.raceTimerEnabled) {
                        VStack(alignment: .leading) {
                            Text("Race Timer")
                                .font(.caption)
                            Text("Countdown with voice")
                                .font(.caption2)
                                .foregroundColor(.gray)
                        }
                    }
                }

                // Timer minutes (only show if race timer enabled)
                if RACE_TIMER_FEATURE_ENABLED && viewModel.raceTimerEnabled {
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

                    // Tap sensitivity (g-force threshold)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Tap Sensitivity (G-force)")
                            .font(.caption)
                            .foregroundColor(.gray)
                        HStack {
                            Button {
                                if viewModel.raceTimerTapGForce > 2 {
                                    viewModel.raceTimerTapGForce -= 1
                                }
                            } label: {
                                Image(systemName: "minus.circle.fill")
                                    .font(.title3)
                                    .foregroundColor(viewModel.raceTimerTapGForce > 2 ? .blue : .gray)
                            }
                            .buttonStyle(.plain)
                            .disabled(viewModel.raceTimerTapGForce <= 2)

                            Text("\(viewModel.raceTimerTapGForce)g")
                                .font(.title3)
                                .frame(minWidth: 40)

                            Button {
                                if viewModel.raceTimerTapGForce < 9 {
                                    viewModel.raceTimerTapGForce += 1
                                }
                            } label: {
                                Image(systemName: "plus.circle.fill")
                                    .font(.title3)
                                    .foregroundColor(viewModel.raceTimerTapGForce < 9 ? .blue : .gray)
                            }
                            .buttonStyle(.plain)
                            .disabled(viewModel.raceTimerTapGForce >= 9)
                        }
                        .frame(maxWidth: .infinity)
                    }
                }

                // Event and Password already appear above - duplicates removed

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
                                // Save password for this event for quick switching
                                PreferencesManager.shared.saveEventPassword(eventId: viewModel.eventId, password: tempPassword)
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
                        // Save password for this event for quick switching
                        PreferencesManager.shared.saveEventPassword(eventId: viewModel.eventId, password: tempPassword)
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
