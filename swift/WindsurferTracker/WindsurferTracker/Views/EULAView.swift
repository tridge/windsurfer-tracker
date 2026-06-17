import SwiftUI

/// EULA acceptance view shown on first launch
struct EULAView: View {
    @Binding var eulaAccepted: Bool
    @State private var scrolledToBottom = false

    var body: some View {
        VStack(spacing: 0) {
            Text("End User License Agreement")
                .font(.headline)
                .padding()

            ScrollView {
                ScrollViewReader { proxy in
                    VStack(alignment: .leading, spacing: 16) {
                        Text(eulaText)
                            .font(.system(.caption, design: .monospaced))

                        Color.clear
                            .frame(height: 1)
                            .id("bottom")
                            .onAppear {
                                scrolledToBottom = true
                            }
                    }
                    .padding()
                }
            }
            .background(Color(.systemGray6))

            VStack(spacing: 12) {
                Text(acceptDisclaimer)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)

                Button(action: {
                    eulaAccepted = true
                }) {
                    Text("Accept")
                        .font(.headline)
                        .foregroundColor(.white)
                        .frame(maxWidth: .infinity)
                        .padding()
                        .background(Color.blue)
                        .cornerRadius(10)
                }
                .padding(.horizontal)
                .padding(.bottom)
            }
            .padding(.top)
            .background(Color(.systemBackground))
        }
    }

    // Accept-button disclaimer — drops the Assist reference in App Store builds.
    private var acceptDisclaimer: String {
        #if !APPSTORE
        return "By tapping Accept, you agree to the terms above, including the important safety disclaimers regarding the Assist feature."
        #else
        return "By tapping Accept, you agree to the terms above."
        #endif
    }

    // EULA sections are assembled and numbered dynamically so the Assist
    // section (and its references) can be compiled out of App Store builds
    // without leaving a numbering gap.
    private var eulaText: String {
        var sections: [(String, String)] = []

        sections.append(("LICENSE GRANT",
            "Subject to the terms of this Agreement, the developer grants you a limited, non-exclusive, non-transferable license to download, install, and use the App for personal, non-commercial purposes on devices you own or control."))

        sections.append(("DESCRIPTION OF SERVICE",
            "Windsurfer Tracker is a GPS tracking application designed for use during sailing and windsurfing events. The App transmits your location to event organizers to enable race tracking, safety monitoring, and event management."))

        #if !APPSTORE
        sections.append(("ASSIST FEATURE - IMPORTANT SAFETY DISCLAIMER",
            """
            ⚠️ THE ASSIST FEATURE IS NOT AN EMERGENCY SERVICE AND IS NOT A SUBSTITUTE FOR CALLING EMERGENCY SERVICES (e.g., 911, 112, 000, or your local emergency number).

            The App includes an "Assist" button that allows users to signal race organizers that they require assistance. By using this feature, you acknowledge and agree that:

            a) Not an Emergency Service: The Assist feature notifies race event organizers only, not emergency services such as coast guard, police, ambulance, or other first responders.

            b) Response Depends on Event Staff: Assistance is provided by race organizers and support boat crews who are monitoring the event. Response times depend on their availability, location, weather conditions, and other factors beyond the control of the App developer.

            c) No Guarantee of Response: While race organizers endeavor to respond to all assist requests promptly, there is no guarantee that assistance will be provided within any specific timeframe or at all.

            d) Verify Event Monitoring: Before relying on the Assist feature, confirm with event organizers that the tracking system is actively monitored during your activity.

            e) Use Emergency Services for Life-Threatening Situations: In any life-threatening emergency, you should immediately contact local emergency services directly rather than relying solely on the Assist feature.

            f) Location Accuracy: GPS location accuracy varies based on device capability, satellite visibility, and environmental conditions. Your reported location may not be precise.

            g) Network Connectivity Required: The Assist feature requires network connectivity (cellular or WiFi) to transmit your request. The feature may not function in areas with poor or no coverage.
            """))
        #endif

        #if !APPSTORE
        let locationData = """
        The App collects and transmits your precise location data to event servers operated by race organizers. This data is used to:
        • Display your position on race tracking maps
        • Enable safety monitoring by event staff
        • Facilitate assistance if you request it

        You consent to this collection and transmission by using the App.
        """
        #else
        let locationData = """
        The App collects and transmits your precise location data to event servers operated by race organizers. This data is used to:
        • Display your position on race tracking maps
        • Enable safety monitoring by event staff

        You consent to this collection and transmission by using the App.
        """
        #endif
        sections.append(("LOCATION DATA", locationData))

        sections.append(("HEALTH AND FITNESS DATA",
            "The App may record workout data to Apple Health, including distance traveled and estimated calories burned during tracking sessions. This data is stored locally on your device and in your personal Health account."))

        sections.append(("ASSUMPTION OF RISK",
            "Sailing, windsurfing, and water sports involve inherent risks including but not limited to drowning, injury, and equipment failure. By using this App, you acknowledge these risks and agree that the App developer is not responsible for any injury, death, or property damage arising from your participation in water sports activities."))

        sections.append(("DISCLAIMER OF WARRANTIES",
            "THE APP IS PROVIDED \"AS IS\" WITHOUT WARRANTY OF ANY KIND. THE DEVELOPER DISCLAIMS ALL WARRANTIES, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT."))

        #if !APPSTORE
        let liability = "TO THE MAXIMUM EXTENT PERMITTED BY LAW, THE DEVELOPER SHALL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, OR ANY LOSS OF PROFITS OR REVENUES, WHETHER INCURRED DIRECTLY OR INDIRECTLY, OR ANY LOSS OF DATA, USE, GOODWILL, OR OTHER INTANGIBLE LOSSES RESULTING FROM YOUR USE OF THE APP OR THE ASSIST FEATURE."
        #else
        let liability = "TO THE MAXIMUM EXTENT PERMITTED BY LAW, THE DEVELOPER SHALL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, OR ANY LOSS OF PROFITS OR REVENUES, WHETHER INCURRED DIRECTLY OR INDIRECTLY, OR ANY LOSS OF DATA, USE, GOODWILL, OR OTHER INTANGIBLE LOSSES RESULTING FROM YOUR USE OF THE APP."
        #endif
        sections.append(("LIMITATION OF LIABILITY", liability))

        sections.append(("INDEMNIFICATION",
            "You agree to indemnify and hold harmless the developer from any claims, damages, losses, or expenses arising from your use of the App or violation of this Agreement."))

        sections.append(("CHANGES TO AGREEMENT",
            "The developer reserves the right to modify this Agreement at any time. Continued use of the App after changes constitutes acceptance of the modified Agreement."))

        sections.append(("GOVERNING LAW",
            "This Agreement shall be governed by the laws of New Zealand."))

        sections.append(("CONTACT",
            "For questions about this Agreement, contact the developer through the App Store."))

        let body = sections.enumerated().map { index, section in
            "\(index + 1). \(section.0)\n\n\(section.1)"
        }.joined(separator: "\n\n")

        return """
        WINDSURFER TRACKER - END USER LICENSE AGREEMENT

        Last Updated: January 2026

        By downloading, installing, or using Windsurfer Tracker ("the App"), you agree to be bound by the terms of this End User License Agreement ("Agreement").

        \(body)
        """
    }
}

#Preview {
    EULAView(eulaAccepted: .constant(false))
}
