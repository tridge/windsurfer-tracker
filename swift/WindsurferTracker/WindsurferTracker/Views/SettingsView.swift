import SwiftUI

/// Full settings sheet
struct SettingsView: View {
    @EnvironmentObject var viewModel: TrackerViewModel
    @Environment(\.dismiss) var dismiss

    @State private var showPassword = false

    var body: some View {
        NavigationView {
            Form {
                // Identity Section - Your Name first (everyone needs this)
                Section("Identity") {
                    HStack {
                        Text("Your Name")
                        Spacer()
                        TextField("e.g., S07", text: $viewModel.sailorId)
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
                            TextField("Optional", text: $viewModel.password)
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 150)
                                .multilineTextAlignment(.trailing)
                                .autocorrectionDisabled()
                                .textInputAutocapitalization(.never)
                        } else {
                            SecureField("Optional", text: $viewModel.password)
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
                        TextField("wstracker.org", text: $viewModel.serverHost)
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
                        TextField("41234", value: $viewModel.serverPort, format: .number.grouping(.never))
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
                    Button("Done") {
                        dismiss()
                    }
                }
            }
            .onAppear {
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
