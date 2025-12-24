#!/usr/bin/env python3
"""
Increment version numbers for Android native, Wear OS, and Swift apps.
Updates versionName/version and versionCode/build number.
All apps are synced to the same version (using max of current versions).
Watch apps (Wear OS and watchOS) get build/code +1 to avoid conflicts.

Usage:
  ./increment_version.py              # Increment minor: 1.8.5 -> 1.9.0
  ./increment_version.py --patch      # Increment patch: 1.8.5 -> 1.8.6
  ./increment_version.py --major      # Increment major: 1.8.5 -> 2.0.0
  ./increment_version.py --set 2.0.0  # Set explicit version
  ./increment_version.py --dry-run    # Show what would change
"""

import argparse
import re
import sys
from pathlib import Path

# File paths (scripts are in scripts/ subdirectory)
ANDROID_GRADLE = Path(__file__).parent.parent / "android/app/build.gradle.kts"
WEAR_GRADLE = Path(__file__).parent.parent / "wear/app/build.gradle.kts"
SWIFT_PROJECT = Path(__file__).parent.parent / "swift/WindsurferTracker/project.yml"


def parse_version_tuple(version_str: str) -> tuple[int, int, int]:
    """Parse version string to tuple for comparison"""
    parts = version_str.split('.')
    if len(parts) == 2:
        return (int(parts[0]), int(parts[1]), 0)
    elif len(parts) == 3:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    return (1, 0, 0)


def parse_android_version(content: str) -> tuple[int, str]:
    """Extract versionCode and versionName from build.gradle.kts"""
    code_match = re.search(r'versionCode\s*=\s*(\d+)', content)
    name_match = re.search(r'versionName\s*=\s*"([^"]+)"', content)

    version_code = int(code_match.group(1)) if code_match else 1
    version_name = name_match.group(1) if name_match else "1.0.0"

    return version_code, version_name


def parse_swift_version(content: str) -> tuple[int, str]:
    """Extract version and build number from project.yml"""
    version_match = re.search(r'MARKETING_VERSION:\s*"([^"]+)"', content)
    code_match = re.search(r'CURRENT_PROJECT_VERSION:\s*"(\d+)"', content)

    version = version_match.group(1) if version_match else "1.0.0"
    build_number = int(code_match.group(1)) if code_match else 1

    return build_number, version


def increment_major_version(version_name: str) -> str:
    """Increment major version: 1.8.5 -> 2.0.0"""
    parts = version_name.split('.')
    major = int(parts[0]) if parts else 1
    return f"{major + 1}.0.0"


def increment_minor_version(version_name: str) -> str:
    """Increment minor version: 1.8.5 -> 1.9.0"""
    parts = version_name.split('.')
    major = int(parts[0]) if len(parts) >= 1 else 1
    minor = int(parts[1]) if len(parts) >= 2 else 0
    return f"{major}.{minor + 1}.0"


def increment_patch_version(version_name: str) -> str:
    """Increment patch version: 1.8.5 -> 1.8.6"""
    parts = version_name.split('.')
    major = int(parts[0]) if len(parts) >= 1 else 1
    minor = int(parts[1]) if len(parts) >= 2 else 0
    patch = int(parts[2]) if len(parts) >= 3 else 0
    return f"{major}.{minor}.{patch + 1}"


def update_android(new_version: str, new_code: int, dry_run: bool = False) -> str:
    """Update Android build.gradle.kts to specified version"""
    content = ANDROID_GRADLE.read_text()
    old_code, old_name = parse_android_version(content)

    new_content = re.sub(
        r'versionCode\s*=\s*\d+',
        f'versionCode = {new_code}',
        content
    )
    new_content = re.sub(
        r'versionName\s*=\s*"[^"]+"',
        f'versionName = "{new_version}"',
        new_content
    )

    if not dry_run:
        ANDROID_GRADLE.write_text(new_content)

    return old_name


def update_wear(new_version: str, new_code: int, dry_run: bool = False) -> str:
    """Update Wear OS build.gradle.kts to specified version"""
    content = WEAR_GRADLE.read_text()
    old_code, old_name = parse_android_version(content)

    new_content = re.sub(
        r'versionCode\s*=\s*\d+',
        f'versionCode = {new_code}',
        content
    )
    new_content = re.sub(
        r'versionName\s*=\s*"[^"]+"',
        f'versionName = "{new_version}"',
        new_content
    )

    if not dry_run:
        WEAR_GRADLE.write_text(new_content)

    return old_name


def update_swift(new_version: str, ios_build: int, watch_build: int, dry_run: bool = False) -> str:
    """Update Swift project.yml to specified version.
    iOS gets ios_build, watchOS gets watch_build (typically +1)."""
    content = SWIFT_PROJECT.read_text()
    old_build, old_version = parse_swift_version(content)

    new_content = re.sub(
        r'MARKETING_VERSION:\s*"[^"]+"',
        f'MARKETING_VERSION: "{new_version}"',
        content
    )
    # Update base CURRENT_PROJECT_VERSION (used by iOS)
    new_content = re.sub(
        r'CURRENT_PROJECT_VERSION:\s*"\d+"',
        f'CURRENT_PROJECT_VERSION: "{ios_build}"',
        new_content
    )

    # Add/update watchOS-specific build number in its settings section
    watch_settings_pattern = r'(WindsurferTrackerWatch:.*?settings:\s*\n\s*base:\s*\n)(.*?)((?=\n\w)|\Z)'

    def add_watch_build(match):
        prefix = match.group(1)
        settings_block = match.group(2)
        suffix = match.group(3)

        # Remove any existing CURRENT_PROJECT_VERSION in watch settings
        settings_block = re.sub(r'\s*CURRENT_PROJECT_VERSION:\s*"\d+"\n?', '', settings_block)

        # Add the watch build number at the start of settings
        return f'{prefix}        CURRENT_PROJECT_VERSION: "{watch_build}"\n{settings_block}{suffix}'

    new_content = re.sub(watch_settings_pattern, add_watch_build, new_content, flags=re.DOTALL)

    if not dry_run:
        SWIFT_PROJECT.write_text(new_content)

    return old_version


def main():
    parser = argparse.ArgumentParser(
        description='Increment version numbers for Android, Wear OS, and Swift apps.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s              Increment minor version (1.8.5 -> 1.9.0)
  %(prog)s --patch      Increment patch version (1.8.5 -> 1.8.6)
  %(prog)s --major      Increment major version (1.8.5 -> 2.0.0)
  %(prog)s --set 2.0.0  Set explicit version number
  %(prog)s --dry-run    Preview changes without modifying files
'''
    )

    increment_group = parser.add_mutually_exclusive_group()
    increment_group.add_argument('--major', action='store_true',
                                  help='Increment major version (X.0.0)')
    increment_group.add_argument('--patch', action='store_true',
                                  help='Increment patch version (x.y.Z)')
    increment_group.add_argument('--set', metavar='VERSION',
                                  help='Set explicit version (e.g., 2.0.0)')

    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would change without modifying files')

    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN - no files will be modified\n")

    # Read current versions from all apps
    android_content = ANDROID_GRADLE.read_text()
    wear_content = WEAR_GRADLE.read_text()
    swift_content = SWIFT_PROJECT.read_text()

    android_code, android_version = parse_android_version(android_content)
    wear_code, wear_version = parse_android_version(wear_content)
    swift_build, swift_version = parse_swift_version(swift_content)

    print(f"Current Android version: {android_version} (code {android_code})")
    print(f"Current Wear OS version: {wear_version} (code {wear_code})")
    print(f"Current Swift version: {swift_version} (build {swift_build})")

    # Find max version for base
    android_tuple = parse_version_tuple(android_version)
    wear_tuple = parse_version_tuple(wear_version)
    swift_tuple = parse_version_tuple(swift_version)

    max_tuple = max(android_tuple, wear_tuple, swift_tuple)
    if android_tuple == max_tuple:
        base_version = android_version
        print(f"\nUsing Android version as base: {base_version}")
    elif wear_tuple == max_tuple:
        base_version = wear_version
        print(f"\nUsing Wear OS version as base: {base_version}")
    else:
        base_version = swift_version
        print(f"\nUsing Swift version as base: {base_version}")

    # Determine new version
    if args.set:
        new_version = args.set
        increment_type = "set"
    elif args.major:
        new_version = increment_major_version(base_version)
        increment_type = "major"
    elif args.patch:
        new_version = increment_patch_version(base_version)
        increment_type = "patch"
    else:
        new_version = increment_minor_version(base_version)
        increment_type = "minor"

    print(f"\nIncrementing {increment_type} version")

    # Use max build/code number + 1 for phone apps, +2 for watch apps
    new_code = max(android_code, wear_code, swift_build) + 1
    watch_code = new_code + 1  # Watch apps get +1 to avoid conflicts

    print(f"\nNew synced version: {new_version}")
    print(f"  Phone apps: code/build {new_code}")
    print(f"  Watch apps: code/build {watch_code}")

    # Update all apps
    print("\nAndroid (build.gradle.kts):")
    old_android = update_android(new_version, new_code, args.dry_run)
    print(f"  Version: {old_android} -> {new_version} (code {new_code})")

    print("\nWear OS (build.gradle.kts):")
    old_wear = update_wear(new_version, watch_code, args.dry_run)
    print(f"  Version: {old_wear} -> {new_version} (code {watch_code})")

    print("\nSwift (project.yml):")
    old_swift = update_swift(new_version, new_code, watch_code, args.dry_run)
    print(f"  iOS: {old_swift} -> {new_version} (build {new_code})")
    print(f"  watchOS: {old_swift} -> {new_version} (build {watch_code})")

    if not args.dry_run:
        print(f"\nVersion updated successfully to {new_version}")
    else:
        print("\nRun without --dry-run to apply changes")


if __name__ == "__main__":
    main()
