import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.collect_iwencai_news import (  # noqa: E402
    NewsCollectionError,
    collect_news,
)


class CollectIwencaiNewsTests(unittest.TestCase):
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
                    "title": "产业新闻",
                    "summary": "摘要",
                    "url": "https://example.test/news",
                    "publish_time": 1786118400,
                }
            ],
        }

    def test_archives_unchanged_payload_without_key(self):
        calls = []

        def fake_request(**kwargs):
            calls.append(kwargs)
            return self.response()

        path = collect_news("A股产业新闻", raw_root=self.root, request=fake_request)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(envelope["payload"], self.response())
        self.assertEqual(calls[0]["api_key"], "secret-not-saved")
        self.assertNotIn("secret-not-saved", path.read_text(encoding="utf-8"))

    def test_saves_gateway_failure_before_raising(self):
        with self.assertRaisesRegex(NewsCollectionError, "saved at"):
            collect_news(
                "query",
                raw_root=self.root,
                request=lambda **kwargs: {"status_code": 2, "data": []},
            )
        self.assertEqual(len(list(self.root.glob("iwencai/*/*/*/*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
