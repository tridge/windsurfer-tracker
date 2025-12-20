import SwiftUI

/// Pulsing assist button with long-press activation
struct AssistButton: View {
    let isActive: Bool
    let onToggle: () -> Void

    @State private var isPulsing = false

    var body: some View {
        Button {
            // Tap does nothing - long press required
        } label: {
            VStack(spacing: 8) {
                if isActive {
                    Text("ASSISTANCE REQUESTED")
                        .font(.headline)
                        .fontWeight(.bold)

                    Text("Long press to cancel")
                        .font(.subheadline)
                } else {
                    Text("REQUEST ASSISTANCE")
                        .font(.headline)
                        .fontWeight(.bold)

                    Text("Long press to activate")
                        .font(.subheadline)
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 24)
            .background(isActive ? Color.red : Color.green)
            .foregroundColor(isActive ? .white : .black)
            .cornerRadius(16)
            .opacity(isActive && isPulsing ? 0.7 : 1.0)
        }
        .simultaneousGesture(
            LongPressGesture(minimumDuration: 0.5)
                .onEnded { _ in
                    // Haptic feedback
                    let generator = UIImpactFeedbackGenerator(style: .heavy)
                    generator.impactOccurred()

                    // Additional vibration pattern
                    if isActive {
                        // Short vibration for cancel
                        let shortGenerator = UIImpactFeedbackGenerator(style: .light)
                        shortGenerator.impactOccurred()
                    } else {
                        // Long vibration pattern for activation
                        let notificationGenerator = UINotificationFeedbackGenerator()
                        notificationGenerator.notificationOccurred(.warning)
                    }

                    onToggle()
                }
        )
        .onChange(of: isActive) { newValue in
            if newValue {
                // Start pulsing animation
                withAnimation(.easeInOut(duration: 0.5).repeatForever(autoreverses: true)) {
                    isPulsing = true
                }
            } else {
                // Stop pulsing
                withAnimation {
                    isPulsing = false
                }
            }
        }
        .onAppear {
            if isActive {
                withAnimation(.easeInOut(duration: 0.5).repeatForever(autoreverses: true)) {
                    isPulsing = true
                }
            }
        }
    }
}

#Preview("Inactive") {
    VStack {
        AssistButton(isActive: false, onToggle: {})
            .padding()
    }
}

#Preview("Active") {
    VStack {
        AssistButton(isActive: true, onToggle: {})
            .padding()
    }
}
