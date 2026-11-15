"""Release transaction checks; no credentials or network access required."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location(
    "publish_playstore", Path(__file__).resolve().parents[1] / "scripts/publish_playstore.py"
)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublishPlayStoreTests(unittest.TestCase):
    def setUp(self):
        self.service = MagicMock()
        self.edits = self.service.edits.return_value
        self.edits.insert.return_value.execute.return_value = {"id": "test-edit"}
        self.edits.tracks.return_value.get.return_value.execute.return_value = {
            "track": "wear:production",
            "releases": [{"status": "completed", "versionCodes": ["151"]}],
        }
        self.edits.bundles.return_value.upload.return_value.execute.return_value = {
            "versionCode": 152
        }
        self.uploads = [(
            "wear", Path("watch.aab"), {"code": 152, "name": "1.11.1"},
            publisher.target_track("wear", "production"),
        )]
        # The optional publishing dependencies are not needed for these tests.
        http = ModuleType("googleapiclient.http")
        http.MediaFileUpload = MagicMock()
        package = ModuleType("googleapiclient")
        package.http = http
        modules = patch.dict(sys.modules, {
            "googleapiclient": package, "googleapiclient.http": http,
        })
        modules.start()
        self.addCleanup(modules.stop)

    def publish(self):
        publisher.publish_edit(self.service, self.uploads, "draft", "Remove heart rate", False)

    def test_watch_draft_preserves_serving_release_and_is_not_sent_for_review(self):
        self.publish()
        update = self.edits.tracks.return_value.update.call_args.kwargs
        self.assertEqual(update["track"], "wear:production")
        self.assertEqual(update["body"]["releases"], [
            {
                "name": "152 (1.11.1)", "versionCodes": ["152"], "status": "draft",
                "releaseNotes": [{"language": "en-AU", "text": "Remove heart rate"}],
            },
            {"status": "completed", "versionCodes": ["151"]},
        ])
        self.edits.tracks.return_value.update.assert_called_once()
        self.edits.validate.assert_not_called()
        self.edits.commit.assert_called_once_with(
            packageName=publisher.PACKAGE_NAME, editId="test-edit",
            changesInReviewBehavior="ERROR_IF_IN_REVIEW", changesNotSentForReview=True,
        )
        self.edits.delete.assert_not_called()

    def test_existing_draft_is_not_overwritten(self):
        self.edits.tracks.return_value.get.return_value.execute.return_value = {
            "releases": [{"status": "draft", "versionCodes": ["150"]}]
        }
        with self.assertRaisesRegex(ValueError, "already has a draft"):
            self.publish()
        self.edits.bundles.return_value.upload.assert_not_called()
        self.edits.commit.assert_not_called()
        self.edits.delete.assert_called_once()

    def test_stale_bundle_is_discarded_without_changing_track(self):
        self.edits.bundles.return_value.upload.return_value.execute.return_value = {
            "versionCode": 151
        }
        with self.assertRaisesRegex(ValueError, "rebuild first"):
            self.publish()
        self.edits.tracks.return_value.update.assert_not_called()
        self.edits.commit.assert_not_called()
        self.edits.delete.assert_called_once()

    def test_commit_failure_discards_edit_without_retrying_review_submission(self):
        self.edits.commit.return_value.execute.side_effect = RuntimeError("Policy declaration required")
        with self.assertRaisesRegex(RuntimeError, "Policy declaration required"):
            self.publish()
        self.edits.commit.assert_called_once()
        self.edits.delete.assert_called_once()

    def test_custom_watch_tracks_and_phone_track_separation(self):
        self.assertEqual(publisher.target_track("wear", "wear:WearOS Track"), "wear:WearOS Track")
        self.assertEqual(publisher.target_track("android", "production"), "production")
        with self.assertRaises(ValueError):
            publisher.target_track("android", "wear:production")


if __name__ == "__main__":
    unittest.main()
