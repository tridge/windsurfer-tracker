import SwiftUI
import WatchKit

/// Watch-optimized assist button with haptic feedback
struct WatchAssistButton: View {
    let isActive: Bool
    let onToggle: () -> Void

    @State private var isPulsing = false

    var body: some View {
        Button {
            // Haptic feedback
            WKInterfaceDevice.current().play(isActive ? .click : .notification)

            onToggle()
        } label: {
            VStack(spacing: 2) {
                if isActive {
                    Text("ASSIST")
                        .font(.caption)
                        .fontWeight(.bold)
                    Text("Tap to cancel")
                        .font(.system(size: 10))
                } else {
                    Text("ASSIST")
                        .font(.caption)
                        .fontWeight(.bold)
                    Text("Tap to request")
                        .font(.system(size: 10))
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
            .background(isActive ? Color.red : Color.green)
            .foregroundColor(isActive ? .white : .black)
            .cornerRadius(12)
            .opacity(isActive && isPulsing ? 0.6 : 1.0)
        }
        .buttonStyle(.plain)
        .onChange(of: isActive) { newValue in
            if newValue {
                withAnimation(.easeInOut(duration: 0.4).repeatForever(autoreverses: true)) {
                    isPulsing = true
                }
            } else {
                withAnimation {
                    isPulsing = false
                }
            }
        }
        .onAppear {
            if isActive {
                withAnimation(.easeInOut(duration: 0.4).repeatForever(autoreverses: true)) {
                    isPulsing = true
                }
            }
        }
    }
}

#Preview("Inactive") {
    WatchAssistButton(isActive: false, onToggle: {})
}

#Preview("Active") {
    WatchAssistButton(isActive: true, onToggle: {})
}
