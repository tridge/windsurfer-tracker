import SwiftUI

/// Compact EULA view for watchOS
struct WatchEULAView: View {
    @Binding var eulaAccepted: Bool

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                Text("Terms of Use")
                    .font(.headline)
                    .bold()

                VStack(alignment: .leading, spacing: 8) {
                    // Assist disclaimer — compiled out of App Store builds.
                    #if !APPSTORE
                    Text("IMPORTANT: ASSIST FEATURE")
                        .font(.caption)
                        .bold()
                        .foregroundColor(.orange)

                    Text("The Assist button notifies race organizers only - NOT emergency services (911/112/000).")
                        .font(.caption2)

                    Text("For life-threatening emergencies, contact emergency services directly.")
                        .font(.caption2)
                        .foregroundColor(.red)

                    Divider()
                    #endif

                    Text("By using this app you agree that:")
                        .font(.caption2)
                        .bold()

                    VStack(alignment: .leading, spacing: 4) {
                        #if !APPSTORE
                        Text("• Assist response depends on event staff availability")
                        Text("• No guarantee of response time")
                        #endif
                        Text("• GPS accuracy varies by conditions")
                        Text("• Network required for tracking")
                        Text("• Water sports involve inherent risks")
                    }
                    .font(.system(size: 11))
                    .foregroundColor(.gray)
                }

                Button {
                    eulaAccepted = true
                } label: {
                    Text("I Agree")
                        .font(.body)
                        .bold()
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(Color.blue)
                        .foregroundColor(.white)
                        .cornerRadius(20)
                }
                .buttonStyle(.plain)
                .padding(.top, 8)

                Text("Full terms at wstracker.org/eula")
                    .font(.system(size: 9))
                    .foregroundColor(.gray)
            }
            .padding(.horizontal, 8)
        }
    }
}

#Preview {
    WatchEULAView(eulaAccepted: .constant(false))
}
