import SwiftUI

/// Full settings sheet
struct SettingsView: View {
    @EnvironmentObject var viewModel: TrackerViewModel
    @Environment(\.dismiss) var dismiss

    @State private var showPassword = true
    @State private var validationError: String? = nil
    @State private var isCheckingPassword = false

    // Local state to avoid updating settings while typing
    @State private var tempSailorId: String = ""
    @State private var tempPassword: String = ""
    @State private var tempServerHost: String = ""
    @State private var tempServerPort: Int = 41234

    // Track original auth values to detect changes
    @State private var originalSailorId: String = ""
    @State private var originalPassword: String = ""
    @State private var originalEventId: Int = 0

    var body: some View {
        NavigationView {
            Form {
                // Identity Section - Your Name first (everyone needs this)
                Section("Identity") {
                    HStack {
                        Text("Your Name")
                        Spacer()
                        TextField("e.g., S07", text: $tempSailorId)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 120)
                            .multilineTextAlignment(.trailing)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                    }
                }

                // Authentication Section - Password needed by most users
                Section("Authentication") {
                    HStack {
                        Text("Password")
                        Spacer()
                        if showPassword {
                            TextField("", text: $tempPassword)
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 150)
                                .multilineTextAlignment(.trailing)
                                .autocorrectionDisabled()
                                .textInputAutocapitalization(.never)
                        } else {
                            SecureField("", text: $tempPassword)
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 150)
                                .multilineTextAlignment(.trailing)
                        }
                    }

                    Toggle("Show Password", isOn: $showPassword)
                }

                // Event Section
                Section("Event") {
                    if viewModel.isLoadingEvents {
                        HStack {
                            Text("Loading events...")
                            Spacer()
                            ProgressView()
                        }
                    } else if viewModel.events.isEmpty {
                        HStack {
                            Text("Event ID")
                            Spacer()
                            TextField("1", value: $viewModel.eventId, format: .number)
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 80)
                                .multilineTextAlignment(.trailing)
                                .keyboardType(.numberPad)
                        }

                        Button("Refresh Events") {
                            Task {
                                await viewModel.fetchEvents()
                            }
                        }
                    } else {
                        Picker("Event", selection: $viewModel.eventId) {
                            ForEach(viewModel.events) { event in
                                Text(event.name).tag(event.eid)
                            }
                        }
                    }
                }

                // Advanced Section - 1Hz mode option
                Section("Advanced") {
                    Toggle("1Hz Mode", isOn: $viewModel.highFrequencyMode)

                    if viewModel.highFrequencyMode {
                        Text("Sends 10 positions per packet for higher precision")
                            .font(.caption)
                            .foregroundColor(.gray)
                    }

                    Toggle("Tracking Buzz", isOn: $viewModel.trackerBeep)

                    Text("Vibrates each minute to remind you the tracker is running")
                        .font(.caption)
                        .foregroundColor(.gray)
                }

                // Role Section - less commonly changed
                Section("Role") {
                    Picker("Role", selection: $viewModel.role) {
                        ForEach(TrackerRole.allCases, id: \.self) { role in
                            Text(role.displayName).tag(role)
                        }
                    }
                }

                // Server Section - rarely changed, at bottom
                Section("Server") {
                    HStack {
                        Text("Host")
                        Spacer()
                        TextField("wstracker.org", text: $tempServerHost)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 180)
                            .multilineTextAlignment(.trailing)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                            .keyboardType(.URL)
                    }

                    HStack {
                        Text("Port")
                        Spacer()
                        TextField("41234", value: $tempServerPort, format: .number.grouping(.never))
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 100)
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.numberPad)
                    }
                }

                // Version Section
                Section {
                    HStack {
                        Text("Version")
                        Spacer()
                        Text(appVersion)
                            .foregroundColor(.gray)
                    }
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button(isCheckingPassword ? "..." : "Done") {
                        // Validate required fields
                        if tempSailorId.isEmpty && tempPassword.isEmpty {
                            validationError = "Name and password are required"
                            return
                        }
                        if tempSailorId.isEmpty {
                            validationError = "Your name is required"
                            return
                        }
                        if tempPassword.isEmpty {
                            validationError = "Password is required"
                            return
                        }

                        // Check if auth fields changed
                        let authFieldsChanged = tempSailorId != originalSailorId ||
                            tempPassword != originalPassword ||
                            viewModel.eventId != originalEventId

                        if authFieldsChanged {
                            // Check password with server
                            isCheckingPassword = true
                            Task { @MainActor in
                                let networkManager = NetworkManager()
                                await networkManager.configure(
                                    host: tempServerHost,
                                    port: UInt16(tempServerPort)
                                )
                                let osVersion = "iOS \(UIDevice.current.systemVersion)"
                                let result = await networkManager.checkPassword(
                                    eventId: viewModel.eventId,
                                    password: tempPassword,
                                    userId: tempSailorId,
                                    userOs: osVersion,
                                    userVer: appVersion
                                )

                                isCheckingPassword = false

                                switch result {
                                case .success:
                                    viewModel.sailorId = tempSailorId
                                    viewModel.password = tempPassword
                                    viewModel.serverHost = tempServerHost
                                    viewModel.serverPort = tempServerPort
                                    dismiss()
                                case .failure(let error):
                                    validationError = error.localizedDescription
                                }
                            }
                        } else {
                            // No auth fields changed, save directly
                            viewModel.sailorId = tempSailorId
                            viewModel.password = tempPassword
                            viewModel.serverHost = tempServerHost
                            viewModel.serverPort = tempServerPort
                            dismiss()
                        }
                    }
                    .disabled(isCheckingPassword)
                }
            }
            .alert("Required Fields", isPresented: .init(
                get: { validationError != nil },
                set: { if !$0 { validationError = nil } }
            )) {
                Button("OK", role: .cancel) { }
            } message: {
                Text(validationError ?? "")
            }
            .onAppear {
                // Load current values into temp state
                tempSailorId = viewModel.sailorId
                tempPassword = viewModel.password
                tempServerHost = viewModel.serverHost
                tempServerPort = viewModel.serverPort
                // Track original auth values
                originalSailorId = viewModel.sailorId
                originalPassword = viewModel.password
                originalEventId = viewModel.eventId
                Task {
                    await viewModel.fetchEvents()
                }
            }
        }
    }

    private var appVersion: String {
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
    SettingsView()
        .environmentObject(TrackerViewModel())
}
