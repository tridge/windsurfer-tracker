import SwiftUI

/// Pre-tracking configuration view - matches Android layout
struct ConfigView: View {
    @EnvironmentObject var viewModel: TrackerViewModel
    @State private var tempSailorId: String = ""
    @State private var tempServerHost: String = ""

    var body: some View {
        VStack(spacing: 0) {
            // Configuration fields
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    // Your Name
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Your Name")
                            .font(.headline)
                            .foregroundColor(.black)

                        TextField("e.g. John or S07", text: $tempSailorId)
                            .font(.title3)
                            .padding(12)
                            .background(Color(white: 0.93))
                            .foregroundColor(.black)
                            .cornerRadius(4)
                    }

                    // Server Address
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Server Address")
                            .font(.headline)
                            .foregroundColor(.black)

                        TextField("IP address or hostname", text: $tempServerHost)
                            .font(.body)
                            .padding(12)
                            .background(Color(white: 0.93))
                            .foregroundColor(.black)
                            .cornerRadius(4)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                    }

                    // Permission warning
                    if viewModel.needsLocationPermission {
                        HStack(spacing: 8) {
                            Image(systemName: "location.slash")
                                .font(.title2)
                                .foregroundColor(.orange)

                            VStack(alignment: .leading, spacing: 2) {
                                Text("Location permission required")
                                    .font(.subheadline)
                                    .fontWeight(.semibold)
                                    .foregroundColor(.orange)

                                Text("Tap 'Start Tracking' to grant permission")
                                    .font(.caption)
                                    .foregroundColor(.gray)
                            }
                        }
                        .padding()
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.orange.opacity(0.1))
                        .cornerRadius(8)
                    }
                }
                .padding(16)
            }

            Spacer()

            // Start button
            Button {
                saveFields()
                viewModel.startTracking()
            } label: {
                Text("Start Tracking")
                    .font(.title3)
                    .fontWeight(.bold)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(Color(white: 0.87))
                    .foregroundColor(.black)
                    .cornerRadius(4)
            }
            .padding(.horizontal, 16)

            // Settings button
            Button {
                saveFields()
                viewModel.showSettings = true
            } label: {
                Text("Settings")
                    .font(.body)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(Color(white: 0.73))
                    .foregroundColor(.black)
                    .cornerRadius(4)
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 16)
        }
        .background(Color.white)
        .onAppear {
            tempSailorId = viewModel.sailorId
            tempServerHost = viewModel.serverHost
        }
        .onChange(of: viewModel.showSettings) { isShowing in
            // Refresh temp values when settings sheet closes
            if !isShowing {
                tempSailorId = viewModel.sailorId
                tempServerHost = viewModel.serverHost
            }
        }
    }

    private func saveFields() {
        viewModel.sailorId = tempSailorId
        viewModel.serverHost = tempServerHost
    }
}

#Preview {
    ConfigView()
        .environmentObject(TrackerViewModel())
}
