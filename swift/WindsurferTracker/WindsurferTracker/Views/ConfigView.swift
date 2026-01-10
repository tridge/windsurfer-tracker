import SwiftUI

/// Pre-tracking configuration view - matches Android layout
struct ConfigView: View {
    @EnvironmentObject var viewModel: TrackerViewModel

    var body: some View {
        VStack(spacing: 0) {
            // Configuration fields - read-only info display
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    // Spacing at top
                    Spacer()
                        .frame(height: 40)

                    // Your Name (label)
                    Text("Your Name")
                        .font(.subheadline)
                        .fontWeight(.bold)
                        .foregroundColor(.black)

                    // Your Name (display value)
                    Text(viewModel.sailorId.isEmpty ? "Not Set" : viewModel.sailorId)
                        .font(.title3)
                        .foregroundColor(.black)
                        .padding(.bottom, 8)

                    // Event (label)
                    Text("Event")
                        .font(.subheadline)
                        .fontWeight(.bold)
                        .foregroundColor(.black)
                        .padding(.top, 8)

                    // Event Display (name + ID)
                    if !viewModel.eventName.isEmpty {
                        Text("\(viewModel.eventName) (ID: \(viewModel.eventId))")
                            .font(.title3)
                            .foregroundColor(Color(red: 0.0, green: 0.4, blue: 0.67))
                    } else {
                        Text("Event \(viewModel.eventId)")
                            .font(.title3)
                            .foregroundColor(Color(red: 0.0, green: 0.4, blue: 0.67))
                    }

                    // Live Tracking (label)
                    Text("Live Tracking")
                        .font(.subheadline)
                        .fontWeight(.bold)
                        .foregroundColor(.black)
                        .padding(.top, 8)

                    // Live Tracking Link (clickable URL only, no label prefix)
                    if let url = URL(string: "https://\(viewModel.serverHost)/event.html?eid=\(viewModel.eventId)") {
                        Link(destination: url) {
                            Text("https://\(viewModel.serverHost)/event.html?eid=\(viewModel.eventId)")
                                .font(.body)
                                .foregroundColor(.blue)
                        }
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
            // Fetch event name async if not already loaded
            Task {
                await viewModel.fetchEventName()
            }
        }
    }
}

#Preview {
    ConfigView()
        .environmentObject(TrackerViewModel())
}
