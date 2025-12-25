#!/usr/bin/env python3
"""
Upload Android and Wear OS AAB files to Google Play Store closed testing.

Setup (one-time):
1. Go to Google Play Console → Setup → API access
2. Click "Create new service account" → follow link to Google Cloud Console
3. Create service account with name like "play-store-upload"
4. Grant role: "Service Account User" (or skip roles)
5. Create JSON key → download as play-store-key.json
6. Back in Play Console, click "Grant access" for the new service account
7. Grant "Release manager" permission for your app
8. Save play-store-key.json in the android/ directory (gitignored)

Usage:
  ./scripts/publish_playstore.py                    # Upload to closed testing
  ./scripts/publish_playstore.py --track internal   # Upload to internal testing
  ./scripts/publish_playstore.py --dry-run          # Show what would be uploaded
"""

import argparse
import sys
from pathlib import Path

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
except ImportError:
    print("ERROR: Required packages not installed. Run:")
    print("  pip install google-api-python-client google-auth")
    sys.exit(1)

# Configuration
PACKAGE_NAME = "nz.co.tracker.windsurfer"
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent

# AAB file locations
ANDROID_AAB = PROJECT_DIR / "android/app/build/outputs/bundle/playstoreRelease/app-playstore-release.aab"
WEAR_AAB = PROJECT_DIR / "wear/app/build/outputs/bundle/release/app-release.aab"

# Service account key location (in android/ directory, gitignored)
SERVICE_ACCOUNT_KEY = PROJECT_DIR / "android/play-store-key.json"


def get_version_info():
    """Extract version info from build.gradle.kts files."""
    import re

    android_gradle = PROJECT_DIR / "android/app/build.gradle.kts"
    wear_gradle = PROJECT_DIR / "wear/app/build.gradle.kts"

    def extract_version(gradle_file):
        content = gradle_file.read_text()
        code_match = re.search(r'versionCode\s*=\s*(\d+)', content)
        name_match = re.search(r'versionName\s*=\s*"([^"]+)"', content)
        return {
            'code': int(code_match.group(1)) if code_match else 0,
            'name': name_match.group(1) if name_match else 'unknown'
        }

    return {
        'android': extract_version(android_gradle),
        'wear': extract_version(wear_gradle)
    }


def upload_to_play_store(track: str = "alpha", dry_run: bool = False):
    """Upload AAB files to Google Play Store.

    Args:
        track: Release track - 'internal', 'alpha' (closed), 'beta' (open), 'production'
        dry_run: If True, just show what would be uploaded
    """

    # Check service account key exists
    if not SERVICE_ACCOUNT_KEY.exists():
        print(f"ERROR: Service account key not found at {SERVICE_ACCOUNT_KEY}")
        print("\nSetup instructions:")
        print("1. Go to Google Play Console → Setup → API access")
        print("2. Create a service account with 'Release manager' permission")
        print("3. Download JSON key and save as: android/play-store-key.json")
        return False

    # Check AAB files exist
    aab_files = []
    versions = get_version_info()

    if ANDROID_AAB.exists():
        aab_files.append(('Android', ANDROID_AAB, versions['android']))
        print(f"✓ Android AAB: {ANDROID_AAB.name} (v{versions['android']['name']}, code {versions['android']['code']})")
    else:
        print(f"✗ Android AAB not found: {ANDROID_AAB}")
        print("  Run: cd android && ./gradlew bundlePlaystoreRelease")

    if WEAR_AAB.exists():
        aab_files.append(('Wear OS', WEAR_AAB, versions['wear']))
        print(f"✓ Wear OS AAB: {WEAR_AAB.name} (v{versions['wear']['name']}, code {versions['wear']['code']})")
    else:
        print(f"✗ Wear OS AAB not found: {WEAR_AAB}")
        print("  Run: cd wear && ./gradlew bundleRelease")

    if not aab_files:
        print("\nERROR: No AAB files found. Build them first.")
        return False

    print(f"\nTarget track: {track}")

    if dry_run:
        print("\n[DRY RUN] Would upload the above files to Google Play Store")
        return True

    # Authenticate with service account
    print("\nAuthenticating with Google Play API...")
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_KEY,
        scopes=['https://www.googleapis.com/auth/androidpublisher']
    )

    service = build('androidpublisher', 'v3', credentials=credentials)

    try:
        # Create a new edit (transaction)
        print("Creating edit...")
        edit_request = service.edits().insert(body={}, packageName=PACKAGE_NAME)
        edit = edit_request.execute()
        edit_id = edit['id']
        print(f"Edit ID: {edit_id}")

        # Upload each AAB
        version_codes = []
        for name, aab_path, version_info in aab_files:
            print(f"\nUploading {name} AAB ({aab_path.stat().st_size / 1024 / 1024:.1f} MB)...")

            media = MediaFileUpload(
                str(aab_path),
                mimetype='application/octet-stream',
                resumable=True
            )

            bundle_response = service.edits().bundles().upload(
                packageName=PACKAGE_NAME,
                editId=edit_id,
                media_body=media
            ).execute()

            version_code = bundle_response['versionCode']
            version_codes.append(version_code)
            print(f"  Uploaded: versionCode={version_code}")

        # Assign to track
        print(f"\nAssigning to '{track}' track...")
        track_response = service.edits().tracks().update(
            packageName=PACKAGE_NAME,
            editId=edit_id,
            track=track,
            body={
                'track': track,
                'releases': [{
                    'versionCodes': version_codes,
                    'status': 'completed'
                }]
            }
        ).execute()
        print(f"  Track updated: {track_response['track']}")

        # Commit the edit
        print("\nCommitting changes...")
        commit_response = service.edits().commit(
            packageName=PACKAGE_NAME,
            editId=edit_id
        ).execute()
        print(f"  Committed edit: {commit_response['id']}")

        print("\n" + "="*50)
        print("SUCCESS! Uploaded to Google Play Store")
        print(f"Track: {track}")
        print(f"Version codes: {version_codes}")
        print("\nNote: It may take a few minutes for the update to appear in Play Console")
        print("="*50)

        return True

    except Exception as e:
        print(f"\nERROR: {e}")
        if 'HttpError' in str(type(e)):
            print("\nCommon issues:")
            print("- Service account doesn't have permission for this app")
            print("- App not set up for the specified track")
            print("- Version code already exists (need to increment)")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Upload Android/Wear OS AABs to Google Play Store',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Tracks:
  internal    Internal testing (up to 100 testers)
  alpha       Closed testing (invite-only, default)
  beta        Open testing (anyone can join)
  production  Production release

Examples:
  %(prog)s                      Upload to closed testing (alpha)
  %(prog)s --track internal     Upload to internal testing
  %(prog)s --dry-run            Show what would be uploaded
'''
    )

    parser.add_argument('--track', default='alpha',
                        choices=['internal', 'alpha', 'beta', 'production'],
                        help='Release track (default: alpha = closed testing)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be uploaded without actually uploading')

    args = parser.parse_args()

    success = upload_to_play_store(track=args.track, dry_run=args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
