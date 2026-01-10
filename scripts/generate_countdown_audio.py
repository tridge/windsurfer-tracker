#!/usr/bin/env python3
"""
Generate pre-recorded audio files for race countdown timer.

Uses gTTS (Google Text-to-Speech) to generate high-quality speech audio files.
Generates files for 1-9 minutes, 30/20 seconds, 10-1 countdown, and "Start".

Requirements:
- Python 3
- gTTS library: pip3 install gTTS
- ffmpeg (for conversion to m4a): apt install ffmpeg

Output format: M4A (AAC) at 44.1kHz mono - optimized for watchOS
"""

import subprocess
import os
import sys
import tempfile

try:
    from gtts import gTTS
except ImportError:
    print("Error: gTTS library not found. Install with: pip3 install gTTS")
    sys.exit(1)

# Output directory for audio files
OUTPUT_DIR = "../swift/WindsurferTracker/WindsurferTrackerWatch/Resources/Audio"

# Language/accent for gTTS (en-US for American English)
LANG = "en"
TLD = "us"  # Top-level domain for accent (us = American)

# Audio files to generate
AUDIO_FILES = {
    # Minutes
    "9_minutes.m4a": "9 minutes",
    "8_minutes.m4a": "8 minutes",
    "7_minutes.m4a": "7 minutes",
    "6_minutes.m4a": "6 minutes",
    "5_minutes.m4a": "5 minutes",
    "4_minutes.m4a": "4 minutes",
    "3_minutes.m4a": "3 minutes",
    "2_minutes.m4a": "2 minutes",
    "1_minute.m4a": "1 minute",

    # Final countdown
    "30_seconds.m4a": "30 seconds",
    "20_seconds.m4a": "20 seconds",
    "10.m4a": "10",
    "9.m4a": "9",
    "8.m4a": "8",
    "7.m4a": "7",
    "6.m4a": "6",
    "5.m4a": "5",
    "4.m4a": "4",
    "3.m4a": "3",
    "2.m4a": "2",
    "1.m4a": "1",
    "start.m4a": "Start!",

    # Reset
    "reset.m4a": "reset",
}


def check_dependencies():
    """Check that required commands are available."""
    # Check for ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("Error: ffmpeg not found.")
        if sys.platform == "darwin":
            print("Install with: brew install ffmpeg")
        else:
            print("Install with: sudo apt install ffmpeg")
        sys.exit(1)


def generate_audio_file(filename, text):
    """Generate an audio file using gTTS and convert to M4A."""
    print(f"Generating {filename}: \"{text}\"")

    # Create output directory if it doesn't exist
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    m4a_path = os.path.join(OUTPUT_DIR, filename)

    # Generate speech using gTTS and save to temporary MP3 file
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
        mp3_path = tmp_file.name

    try:
        # Generate speech
        tts = gTTS(text=text, lang=LANG, tld=TLD, slow=False)
        tts.save(mp3_path)

        # Convert MP3 to M4A using ffmpeg
        subprocess.run(
            [
                "ffmpeg", "-y",  # Overwrite output files
                "-i", mp3_path,
                "-c:a", "aac",  # AAC codec
                "-b:a", "64k",  # 64kbps bitrate (good quality, small size)
                "-ar", "44100",  # 44.1kHz sample rate
                "-ac", "1",  # Mono
                m4a_path
            ],
            capture_output=True,
            check=True
        )

        print(f"  ✓ Created {filename}")
    finally:
        # Remove temporary MP3 file
        if os.path.exists(mp3_path):
            os.remove(mp3_path)


def main():
    """Generate all audio files."""
    print("Race Countdown Audio Generator")
    print("=" * 50)

    check_dependencies()

    print(f"\nGenerating {len(AUDIO_FILES)} audio files...")
    print(f"Language: {LANG} (accent: {TLD})")
    print(f"Output: {OUTPUT_DIR}\n")

    for filename, text in AUDIO_FILES.items():
        try:
            generate_audio_file(filename, text)
        except subprocess.CalledProcessError as e:
            print(f"  ✗ Failed to generate {filename}: {e}")
            sys.exit(1)

    print(f"\n✓ Successfully generated {len(AUDIO_FILES)} audio files")
    print(f"\nFiles saved to: {OUTPUT_DIR}")
    print("\nNext steps:")
    print("1. Verify audio files sound correct")
    print("2. Add audio files to Xcode project (WindsurferTrackerWatch target)")
    print("3. Update WatchTrackerViewModel to use AVAudioPlayer")


if __name__ == "__main__":
    main()
