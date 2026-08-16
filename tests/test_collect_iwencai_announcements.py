import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.collect_iwencai_announcements import (  # noqa: E402
    AnnouncementCollectionError,
    collect_announcements,
)


class CollectIwencaiAnnouncementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "raw"
        self.key = patch.dict(os.environ, {"IWENCAI_API_KEY": "secret-not-saved"})
        self.key.start()
        self.addCleanup(self.key.stop)

    @staticmethod
    def response():
        return {
            "status_code": 0,
            "data": [
                {
                    "title": "某公司关于股份回购的公告",
                    "summary": "公告摘要",
                    "url": "https://example.test/a",
                    "publish_time": 1786118400000,
                }
            ],
        }

    def test_archives_response_unchanged_without_key(self):
        calls = []

        def fake_request(**kwargs):
            calls.append(kwargs)
            return self.response()

        path = collect_announcements(
            "A股 股份回购公告",
            raw_root=self.root,
            request=fake_request,
        )
        envelope = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(envelope["payload"], self.response())
        self.assertEqual(calls[0]["api_key"], "secret-not-saved")
        self.assertNotIn("secret-not-saved", path.read_text(encoding="utf-8"))

    def test_saves_gateway_failure_before_raising(self):
        with self.assertRaisesRegex(AnnouncementCollectionError, "saved at"):
            collect_announcements(
                "query",
                raw_root=self.root,
                request=lambda **kwargs: {"status_code": 1, "data": []},
            )
        self.assertEqual(len(list(self.root.glob("iwencai/*/*/*/*.json"))), 1)

    def test_rejects_sensitive_response_before_public_raw_write(self):
        response = self.response()
        response["metadata"] = {"Authorization": "test-only-marker"}
        with self.assertRaisesRegex(
            AnnouncementCollectionError, "forbidden credential field"
        ):
            collect_announcements(
                "query",
                raw_root=self.root,
                request=lambda **kwargs: response,
            )
        self.assertEqual(list(self.root.glob("iwencai/*/*/*/*.json")), [])

    def test_requires_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(AnnouncementCollectionError, "IWENCAI_API_KEY"):
                collect_announcements(
                    "query",
                    raw_root=self.root,
                    request=lambda **kwargs: self.response(),
                )


if __name__ == "__main__":
    unittest.main()
