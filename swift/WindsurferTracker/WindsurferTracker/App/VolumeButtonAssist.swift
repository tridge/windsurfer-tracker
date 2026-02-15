import Foundation
import AVFoundation
import MediaPlayer
import UIKit

/// Detects volume up+down button combo for emergency assist toggle.
///
/// Uses AVAudioSession KVO on outputVolume to detect button presses.
/// An offscreen MPVolumeView suppresses the system volume HUD and provides
/// a slider to nudge volume away from 0/max so both directions always register.
/// A silent audio loop keeps the session active when the screen is locked.
class VolumeButtonAssist {
    private let audioSession = AVAudioSession.sharedInstance()
    private var volumeObservation: NSKeyValueObservation?
    private var volumeView: MPVolumeView?
    private var volumeSlider: UISlider?
    private var silentPlayer: AVAudioPlayer?

    // Track last known volume to detect direction
    private var lastVolume: Float = 0.5

    // Combo detection state
    private var lastDirectionUp: Bool?
    private var lastChangeTime: TimeInterval = 0
    private var comboDetectedTime: TimeInterval = 0  // Debounce
    private let comboWindowSeconds: TimeInterval = 0.5

    // Counter for programmatic volume changes to ignore in KVO
    private var ignoreCount = 0

    /// Called on main thread when volume up+down combo is detected
    var onComboDetected: (() -> Void)?

    func start() {
        // Already active
        guard volumeObservation == nil else { return }

        do {
            try audioSession.setCategory(.playback, options: .mixWithOthers)
            try audioSession.setActive(true)
        } catch {
            print("[VolumeAssist] Error activating audio session: \(error)")
        }

        lastVolume = audioSession.outputVolume

        // Start silent audio loop to keep audio session active when screen is locked.
        // Without this, iOS deactivates the session and KVO stops firing.
        startSilentAudio()

        // Create offscreen MPVolumeView to suppress volume HUD and access slider
        let view = MPVolumeView(frame: CGRect(x: -3000, y: -3000, width: 1, height: 1))
        view.showsRouteButton = false
        view.isHidden = false  // Must not be hidden for slider to work
        view.alpha = 0.01  // Nearly invisible but not hidden

        // Add to key window
        if let window = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first?.windows.first {
            window.addSubview(view)
        }
        volumeView = view

        // Find the UISlider for programmatic volume control
        volumeSlider = findSlider(in: view)

        // Nudge to safe volume if at edges
        nudgeVolume()

        // Start observing volume changes
        volumeObservation = audioSession.observe(\.outputVolume, options: [.new]) { [weak self] _, change in
            guard let self = self,
                  let newVolume = change.newValue,
                  newVolume != self.lastVolume else { return }

            // Ignore programmatic volume changes (nudges, tone volume)
            if self.ignoreCount > 0 {
                self.ignoreCount -= 1
                self.lastVolume = newVolume
                return
            }

            let directionUp = newVolume > self.lastVolume
            let now = ProcessInfo.processInfo.systemUptime

            // Check for combo: opposite direction within window
            if let lastDir = self.lastDirectionUp,
               lastDir != directionUp,
               (now - self.lastChangeTime) <= self.comboWindowSeconds,
               (now - self.comboDetectedTime) > 1.0 {  // 1s debounce
                self.comboDetectedTime = now
                // Reset direction state so next combo needs fresh pair
                self.lastDirectionUp = nil
                DispatchQueue.main.async {
                    self.onComboDetected?()
                }
            } else {
                self.lastDirectionUp = directionUp
            }

            self.lastChangeTime = now
            self.lastVolume = newVolume

            // Nudge volume back to middle if at edges
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
                self?.nudgeVolume()
            }
        }
    }

    func stop() {
        volumeObservation?.invalidate()
        volumeObservation = nil
        silentPlayer?.stop()
        silentPlayer = nil
        volumeView?.removeFromSuperview()
        volumeView = nil
        volumeSlider = nil
    }

    /// Set system volume programmatically, ignoring the change in KVO.
    /// Used to crank volume for assist tones and restore afterward.
    func setSystemVolume(_ level: Float) {
        ignoreCount += 1
        volumeSlider?.value = level
    }

    // MARK: - Silent Audio

    /// Play a silent audio loop to keep the AVAudioSession active when the screen is locked.
    private func startSilentAudio() {
        // Generate 1 second of silence as WAV
        let sampleRate = 44100
        let numSamples = sampleRate  // 1 second
        var data = Data()

        // WAV header
        let dataSize = numSamples * 2
        let fileSize = UInt32(36 + dataSize)
        data.append(contentsOf: [0x52, 0x49, 0x46, 0x46])  // "RIFF"
        appendUInt32(&data, fileSize)
        data.append(contentsOf: [0x57, 0x41, 0x56, 0x45])  // "WAVE"
        data.append(contentsOf: [0x66, 0x6D, 0x74, 0x20])  // "fmt "
        appendUInt32(&data, 16)
        appendUInt16(&data, 1)   // PCM
        appendUInt16(&data, 1)   // mono
        appendUInt32(&data, UInt32(sampleRate))
        appendUInt32(&data, UInt32(sampleRate * 2))
        appendUInt16(&data, 2)   // block align
        appendUInt16(&data, 16)  // bits per sample
        data.append(contentsOf: [0x64, 0x61, 0x74, 0x61])  // "data"
        appendUInt32(&data, UInt32(dataSize))

        // Silent samples
        data.append(Data(count: dataSize))

        do {
            silentPlayer = try AVAudioPlayer(data: data)
            silentPlayer?.numberOfLoops = -1  // Loop forever
            silentPlayer?.volume = 0.0
            silentPlayer?.play()
        } catch {
            print("[VolumeAssist] Failed to start silent audio: \(error)")
        }
    }

    private func appendUInt32(_ data: inout Data, _ value: UInt32) {
        var v = value.littleEndian
        data.append(Data(bytes: &v, count: 4))
    }

    private func appendUInt16(_ data: inout Data, _ value: UInt16) {
        var v = value.littleEndian
        data.append(Data(bytes: &v, count: 2))
    }

    // MARK: - Volume Control

    /// Nudge volume to 0.5 if it's at or near the edges (0 or max)
    private func nudgeVolume() {
        let current = audioSession.outputVolume
        let step: Float = 0.0625  // One volume step is 1/16
        if current <= step || current >= (1.0 - step) {
            ignoreCount += 1
            volumeSlider?.value = 0.5
        }
    }

    /// Recursively find UISlider in a view hierarchy (MPVolumeView may nest it)
    private func findSlider(in view: UIView) -> UISlider? {
        for subview in view.subviews {
            if let slider = subview as? UISlider {
                return slider
            }
            if let found = findSlider(in: subview) {
                return found
            }
        }
        return nil
    }
}
