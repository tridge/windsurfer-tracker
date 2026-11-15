#!/usr/bin/env python3
"""Upload phone and Wear OS bundles to their respective Google Play tracks.

One-time setup:
1. Enable the Google Play Android Developer API in Google Cloud.
2. Create a service account and download its JSON key.
3. Invite its email in Play Console's Users and permissions, granting app-level
   access to view app information and release to the required tracks.
4. Save the key as android/play-store-key.json (gitignored).
5. Install google-api-python-client and google-auth in a Python environment.

Releases default to drafts. Health app declarations must be updated in Play
Console separately. Examples:
  ./scripts/publish_playstore.py --platform wear --track production --dry-run
  ./scripts/publish_playstore.py --platform wear --track production
  ./scripts/publish_playstore.py --platform wear --track production --release-status completed
"""

import argparse
import re
import sys
from pathlib import Path


PACKAGE_NAME = "nz.co.tracker.windsurfer"
PROJECT_DIR = Path(__file__).resolve().parent.parent
ANDROID_AAB = PROJECT_DIR / "android/app/build/outputs/bundle/playstoreRelease/app-playstore-release.aab"
WEAR_AAB = PROJECT_DIR / "wear/app/build/outputs/bundle/playstoreRelease/app-playstore-release.aab"
SERVICE_ACCOUNT_KEY = PROJECT_DIR / "android/play-store-key.json"


def get_version_info():
    versions = {}
    for platform, directory in [("android", "android"), ("wear", "wear")]:
        content = (PROJECT_DIR / directory / "app/build.gradle.kts").read_text()
        versions[platform] = {
            "code": int(re.search(r"versionCode\s*=\s*(\d+)", content).group(1)),
            "name": re.search(r'versionName\s*=\s*"([^"]+)"', content).group(1),
        }
    return versions


def target_track(platform, track):
    if platform == "wear":
        return track if track.startswith("wear:") else f"wear:{track}"
    if ":" in track:
        raise ValueError("Select --platform wear when specifying a wear: track")
    return track


def release_body(track, version, status, existing, notes=None):
    # Preserve the serving release while creating a draft. An existing draft
    # needs to be resolved in Console first, rather than silently overwritten.
    if any(r.get("status") == "draft" for r in existing.get("releases", [])):
        raise ValueError(f"Track {track} already has a draft; resolve it in Play Console first")
    release = {
        "name": f"{version['code']} ({version['name']})",
        "versionCodes": [str(version["code"])],
        "status": status,
    }
    if notes:
        release["releaseNotes"] = [{"language": "en-AU", "text": notes}]
    releases = [release]
    if status == "draft":
        releases.extend(existing.get("releases", []))
    return {"track": track, "releases": releases}


def publish_edit(service, uploads, status, notes, no_review):
    """Upload in a single edit; discard all uncommitted changes on failure."""
    from googleapiclient.http import MediaFileUpload

    edits = service.edits()
    edit_id = edits.insert(packageName=PACKAGE_NAME, body={}).execute()["id"]
    committed = False
    try:
        # Validate every target before uploading any bundles.
        bodies = []
        for platform, path, version, track in uploads:
            existing = edits.tracks().get(
                packageName=PACKAGE_NAME, editId=edit_id, track=track
            ).execute()
            bodies.append(release_body(track, version, status, existing, notes))

        for (platform, path, version, track), body in zip(uploads, bodies):
            print(f"Uploading {platform} {version['name']} ({version['code']}) to {track}...", flush=True)
            bundle = edits.bundles().upload(
                packageName=PACKAGE_NAME,
                editId=edit_id,
                media_body=MediaFileUpload(
                    str(path), mimetype="application/octet-stream", resumable=True
                ),
            ).execute()
            if int(bundle["versionCode"]) != version["code"]:
                raise ValueError(f"Bundle version {bundle['versionCode']} does not match source version {version['code']}; rebuild first")
            edits.tracks().update(
                packageName=PACKAGE_NAME, editId=edit_id, track=track, body=body
            ).execute()

        # Commit validates the edit and accepts changesNotSentForReview. The
        # separate validate endpoint rejects pending policy fixes because it
        # cannot accept that flag.
        commit_args = {
            "packageName": PACKAGE_NAME,
            "editId": edit_id,
            "changesInReviewBehavior": "ERROR_IF_IN_REVIEW",
        }
        if status == "draft" or no_review:
            commit_args["changesNotSentForReview"] = True
        edits.commit(**commit_args).execute()
        committed = True
        if status == "draft":
            print("Saved draft releases; not submitted for review.")
        elif no_review:
            print("Saved releases without submitting for review.")
        else:
            print("Committed release changes for Google Play processing.")
    finally:
        if not committed:
            try:
                edits.delete(packageName=PACKAGE_NAME, editId=edit_id).execute()
            except Exception as error:
                print(f"Could not discard edit {edit_id}: {error}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", choices=["android", "wear", "both"], default="both")
    parser.add_argument("--track", default="alpha", help="Track name; wear: prefix is added for watch releases")
    parser.add_argument("--release-status", choices=["draft", "completed"], default="draft")
    parser.add_argument("--release-notes-file", type=Path, help="UTF-8 release notes (en-AU)")
    parser.add_argument("--credentials", type=Path, default=SERVICE_ACCOUNT_KEY)
    parser.add_argument("--no-review", action="store_true", help="Save changes without submitting for review")
    parser.add_argument("--dry-run", action="store_true", help="Check local files and show the plan without API access")
    args = parser.parse_args()

    try:
        versions = get_version_info()
        uploads = []
        for platform, path in [("android", ANDROID_AAB), ("wear", WEAR_AAB)]:
            if args.platform not in (platform, "both"):
                continue
            if not path.is_file():
                raise ValueError(f"Bundle not found: {path}; build bundlePlaystoreRelease first")
            track = target_track(platform, args.track)
            uploads.append((platform, path, versions[platform], track))
            print(f"{platform}: {path}\n  Version {versions[platform]['name']} ({versions[platform]['code']}), track {track}, status {args.release_status}")
        notes = args.release_notes_file.read_text(encoding="utf-8").strip() if args.release_notes_file else None
        if args.dry_run:
            print("Dry run: no API requests made.")
            return 0
        if not args.credentials.is_file():
            raise ValueError(f"Service account key not found: {args.credentials}")

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            args.credentials, scopes=["https://www.googleapis.com/auth/androidpublisher"]
        )
        service = build("androidpublisher", "v3", credentials=credentials, cache_discovery=False)
        publish_edit(service, uploads, args.release_status, notes, args.no_review)
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
