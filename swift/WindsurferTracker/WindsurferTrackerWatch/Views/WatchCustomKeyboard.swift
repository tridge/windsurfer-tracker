import SwiftUI

/// Custom QWERTY keyboard for Apple Watch Series 6 and earlier (no system keyboard)
struct WatchCustomKeyboard: View {
    let title: String
    @Binding var text: String
    var onDone: () -> Void

    @State private var isShifted = false
    @State private var showNumbers = false

    private let letterRows: [[String]] = [
        ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
        ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
        ["z", "x", "c", "v", "b", "n", "m"]
    ]

    private let numberRow1 = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]
    private let symbolRow = ["-", "_", ".", ",", ":", "/", "@", "!"]

    var body: some View {
        GeometryReader { geo in
            let keyWidth = (geo.size.width - 4) / 10
            let keyHeight: CGFloat = 28

            VStack(spacing: 0) {
                // Text display
                HStack {
                    Text(text.isEmpty ? title : text)
                        .font(.system(size: 14))
                        .foregroundColor(text.isEmpty ? .gray : .white)
                        .lineLimit(1)
                        .truncationMode(.head)
                    Spacer()
                }
                .padding(.horizontal, 4)
                .padding(.vertical, 4)
                .background(Color.black.opacity(0.3))
                .cornerRadius(4)

                Spacer().frame(height: 4)

                if showNumbers {
                    numberKeyboard(keyWidth: keyWidth, keyHeight: keyHeight, totalWidth: geo.size.width)
                } else {
                    letterKeyboard(keyWidth: keyWidth, keyHeight: keyHeight, totalWidth: geo.size.width)
                }
            }
        }
        .navigationTitle(title)
    }

    // MARK: - Letter keyboard

    @ViewBuilder
    private func letterKeyboard(keyWidth: CGFloat, keyHeight: CGFloat, totalWidth: CGFloat) -> some View {
        // Row 1: q w e r t y u i o p
        HStack(spacing: 0) {
            ForEach(letterRows[0], id: \.self) { key in
                keyButton(displayKey(key), keyWidth: keyWidth, keyHeight: keyHeight) {
                    appendChar(key)
                }
            }
        }

        // Row 2: a s d f g h j k l (centered)
        HStack(spacing: 0) {
            Spacer().frame(width: keyWidth / 2)
            ForEach(letterRows[1], id: \.self) { key in
                keyButton(displayKey(key), keyWidth: keyWidth, keyHeight: keyHeight) {
                    appendChar(key)
                }
            }
            Spacer().frame(width: keyWidth / 2)
        }

        // Row 3: shift + z x c v b n m + backspace
        HStack(spacing: 0) {
            // Shift key
            keyButton("\u{21E7}", keyWidth: keyWidth * 1.3, keyHeight: keyHeight, bg: isShifted ? .blue : .gray.opacity(0.4)) {
                isShifted.toggle()
            }
            ForEach(letterRows[2], id: \.self) { key in
                keyButton(displayKey(key), keyWidth: keyWidth, keyHeight: keyHeight) {
                    appendChar(key)
                }
            }
            // Backspace
            keyButton("\u{232B}", keyWidth: keyWidth * 1.7, keyHeight: keyHeight, bg: .gray.opacity(0.4)) {
                if !text.isEmpty {
                    text.removeLast()
                }
            }
        }

        // Row 4: 123, space, ., Done
        HStack(spacing: 0) {
            keyButton("123", keyWidth: keyWidth * 2.2, keyHeight: keyHeight, bg: .gray.opacity(0.4)) {
                showNumbers = true
            }
            keyButton("", keyWidth: keyWidth * 4.8, keyHeight: keyHeight, bg: .gray.opacity(0.25)) {
                text.append(" ")
            }
            keyButton(".", keyWidth: keyWidth * 1, keyHeight: keyHeight) {
                text.append(".")
            }
            keyButton("Done", keyWidth: keyWidth * 2, keyHeight: keyHeight, bg: .blue) {
                onDone()
            }
        }
    }

    // MARK: - Number/symbol keyboard

    @ViewBuilder
    private func numberKeyboard(keyWidth: CGFloat, keyHeight: CGFloat, totalWidth: CGFloat) -> some View {
        // Row 1: 1 2 3 4 5 6 7 8 9 0
        HStack(spacing: 0) {
            ForEach(numberRow1, id: \.self) { key in
                keyButton(key, keyWidth: keyWidth, keyHeight: keyHeight) {
                    text.append(key)
                }
            }
        }

        // Row 2: - _ . , : / @ !
        HStack(spacing: 0) {
            Spacer().frame(width: keyWidth)
            ForEach(symbolRow, id: \.self) { key in
                keyButton(key, keyWidth: keyWidth, keyHeight: keyHeight) {
                    text.append(key)
                }
            }
            Spacer().frame(width: keyWidth)
        }

        // Row 3: abc + space + backspace
        HStack(spacing: 0) {
            keyButton("abc", keyWidth: keyWidth * 2.2, keyHeight: keyHeight, bg: .gray.opacity(0.4)) {
                showNumbers = false
            }
            keyButton("", keyWidth: keyWidth * 5.8, keyHeight: keyHeight, bg: .gray.opacity(0.25)) {
                text.append(" ")
            }
            keyButton("\u{232B}", keyWidth: keyWidth * 2, keyHeight: keyHeight, bg: .gray.opacity(0.4)) {
                if !text.isEmpty {
                    text.removeLast()
                }
            }
        }

        // Row 4: Done (full width)
        HStack(spacing: 0) {
            keyButton("Done", keyWidth: keyWidth * 10, keyHeight: keyHeight, bg: .blue) {
                onDone()
            }
        }
    }

    // MARK: - Helpers

    private func displayKey(_ key: String) -> String {
        isShifted ? key.uppercased() : key
    }

    private func appendChar(_ key: String) {
        let char = isShifted ? key.uppercased() : key
        text.append(char)
        if isShifted {
            isShifted = false
        }
    }

    private func keyButton(_ label: String, keyWidth: CGFloat, keyHeight: CGFloat, bg: Color = .gray.opacity(0.3), action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(.system(size: 13, weight: .medium))
                .frame(width: keyWidth - 2, height: keyHeight)
                .background(bg)
                .foregroundColor(.white)
                .cornerRadius(3)
        }
        .buttonStyle(.plain)
    }
}
