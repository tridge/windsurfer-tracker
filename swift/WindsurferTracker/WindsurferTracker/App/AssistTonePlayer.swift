import Foundation
import AVFoundation

/// Generates and plays ascending/descending tone sequences for audio feedback.
/// 3-tone (low-mid-high) for assist activation/deactivation.
/// 2-tone (low-high) for tracking start/stop.
/// Matches the Android ToneGenerator DTMF 1/5/9 pattern.
class AssistTonePlayer {
    private var player: AVAudioPlayer?

    // Frequencies chosen to be clearly audible and distinct
    // Similar pitch spacing to Android DTMF tones 1/5/9
    private static let lowFreq = 880.0    // A5
    private static let midFreq = 1175.0   // D6
    private static let highFreq = 1568.0  // G6

    private static let toneDuration = 0.15  // 150ms per tone
    private static let gapDuration = 0.05   // 50ms gap between tones
    private static let sampleRate = 44100.0

    /// Ensure audio session is configured for playback before playing tones.
    /// VolumeButtonAssist may not have started yet (e.g. on start tracking).
    private func ensureAudioSession() {
        let session = AVAudioSession.sharedInstance()
        if session.category != .playback {
            try? session.setCategory(.playback, options: .mixWithOthers)
        }
        try? session.setActive(true)
    }

    /// Play ascending (activate) or descending (deactivate) 3-tone assist sequence
    func play(ascending: Bool) {
        ensureAudioSession()
        let freqs: [Double]
        if ascending {
            freqs = [Self.lowFreq, Self.midFreq, Self.highFreq]
        } else {
            freqs = [Self.highFreq, Self.midFreq, Self.lowFreq]
        }

        let data = generateWAV(frequencies: freqs)
        do {
            player = try AVAudioPlayer(data: data)
            player?.volume = 1.0
            player?.play()
        } catch {
            NSLog("[AssistTone] Failed to play: \(error)")
        }
    }

    /// Play ascending or descending 2-tone sequence for tracking start/stop
    func playDouble(ascending: Bool) {
        ensureAudioSession()
        let freqs: [Double]
        if ascending {
            freqs = [Self.lowFreq, Self.highFreq]
        } else {
            freqs = [Self.highFreq, Self.lowFreq]
        }

        let data = generateWAV(frequencies: freqs)
        do {
            player = try AVAudioPlayer(data: data)
            player?.volume = 1.0
            player?.play()
        } catch {
            NSLog("[AssistTone] Failed to play double tone: \(error)")
        }
    }

    /// Play quad-beep (high-low-high-low) for support boat alert when a sailor has active assist
    func playQuadBeep() {
        ensureAudioSession()
        let freqs: [Double] = [Self.highFreq, Self.lowFreq, Self.highFreq, Self.lowFreq]

        let data = generateWAV(frequencies: freqs)
        do {
            player = try AVAudioPlayer(data: data)
            player?.volume = 1.0
            player?.play()
        } catch {
            NSLog("[AssistTone] Failed to play quad beep: \(error)")
        }
    }

    func stop() {
        player?.stop()
        player = nil
    }

    /// Generate a WAV file in memory containing a multi-tone sequence
    private func generateWAV(frequencies: [Double]) -> Data {
        let sr = Self.sampleRate
        let toneSamples = Int(sr * Self.toneDuration)
        let gapSamples = Int(sr * Self.gapDuration)
        let totalSamples = frequencies.count * toneSamples + (frequencies.count - 1) * gapSamples

        var samples = [Int16]()
        samples.reserveCapacity(totalSamples)

        for (i, freq) in frequencies.enumerated() {
            // Generate sine wave for this tone
            for j in 0..<toneSamples {
                let t = Double(j) / sr
                // Apply short fade in/out (2ms) to avoid clicks
                let fadeLen = Int(sr * 0.002)
                var amplitude = 1.0
                if j < fadeLen {
                    amplitude = Double(j) / Double(fadeLen)
                } else if j > toneSamples - fadeLen {
                    amplitude = Double(toneSamples - j) / Double(fadeLen)
                }
                let value = sin(2.0 * .pi * freq * t) * amplitude
                samples.append(Int16(value * 30000))
            }
            // Add silence gap (except after last tone)
            if i < frequencies.count - 1 {
                samples.append(contentsOf: [Int16](repeating: 0, count: gapSamples))
            }
        }

        return createWAVData(samples: samples, sampleRate: Int(sr))
    }

    /// Create WAV file data from 16-bit PCM samples
    private func createWAVData(samples: [Int16], sampleRate: Int) -> Data {
        var data = Data()
        let dataSize = samples.count * 2
        let fileSize = UInt32(36 + dataSize)

        // RIFF header
        data.append(contentsOf: [0x52, 0x49, 0x46, 0x46])  // "RIFF"
        appendUInt32(&data, fileSize)
        data.append(contentsOf: [0x57, 0x41, 0x56, 0x45])  // "WAVE"

        // fmt chunk
        data.append(contentsOf: [0x66, 0x6D, 0x74, 0x20])  // "fmt "
        appendUInt32(&data, 16)                              // chunk size
        appendUInt16(&data, 1)                               // PCM format
        appendUInt16(&data, 1)                               // mono
        appendUInt32(&data, UInt32(sampleRate))              // sample rate
        appendUInt32(&data, UInt32(sampleRate * 2))          // byte rate
        appendUInt16(&data, 2)                               // block align
        appendUInt16(&data, 16)                              // bits per sample

        // data chunk
        data.append(contentsOf: [0x64, 0x61, 0x74, 0x61])  // "data"
        appendUInt32(&data, UInt32(dataSize))

        for sample in samples {
            appendInt16(&data, sample)
        }

        return data
    }

    private func appendUInt32(_ data: inout Data, _ value: UInt32) {
        var v = value.littleEndian
        data.append(Data(bytes: &v, count: 4))
    }

    private func appendUInt16(_ data: inout Data, _ value: UInt16) {
        var v = value.littleEndian
        data.append(Data(bytes: &v, count: 2))
    }

    private func appendInt16(_ data: inout Data, _ value: Int16) {
        var v = value.littleEndian
        data.append(Data(bytes: &v, count: 2))
    }
}
