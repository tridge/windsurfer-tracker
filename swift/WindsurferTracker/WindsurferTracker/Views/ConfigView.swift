import SwiftUI

/// Pre-tracking configuration view - matches Android layout
struct ConfigView: View {
    @EnvironmentObject var viewModel: TrackerViewModel
    @State private var tempSailorId: String = ""

    var body: some View {
        VStack(spacing: 0) {
            // Configuration fields
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    // Spacing at top
                    Spacer()
                        .frame(height: 24)

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

                    // Event Display (name + ID)
                    if !viewModel.eventName.isEmpty {
                        Text("\(viewModel.eventName) (ID: \(viewModel.eventId))")
                            .font(.body)
                            .fontWeight(.bold)
                            .foregroundColor(Color(red: 0.0, green: 0.4, blue: 0.67))
                    } else {
                        Text("Event \(viewModel.eventId)")
                            .font(.body)
                            .fontWeight(.bold)
                            .foregroundColor(Color(red: 0.0, green: 0.4, blue: 0.67))
                    }

                    // Live Tracking Link (opens in default browser)
                    if let url = URL(string: "https://\(viewModel.serverHost)/event.html?eid=\(viewModel.eventId)") {
                        Link(destination: url) {
                            Text("Live Tracking: \(viewModel.serverHost)/event.html?eid=\(viewModel.eventId)")
                                .font(.caption)
                                .foregroundColor(.blue)
                        }
                    }

                    // Server Address removed - now only in Settings

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
        }
        .onChange(of: viewModel.showSettings) { isShowing in
            // Refresh temp values when settings sheet closes
            if !isShowing {
                tempSailorId = viewModel.sailorId
            }
        }
    }

    private func saveFields() {
        viewModel.sailorId = tempSailorId
        // Server host is now only in Settings, not on config screen
    }
}

#Preview {
    ConfigView()
        .environmentObject(TrackerViewModel())
}
