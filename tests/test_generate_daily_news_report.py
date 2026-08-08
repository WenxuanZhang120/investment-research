import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.generate_daily_news_report import (  # noqa: E402
    NewsReportError,
    load_news,
    render_report,
)


class GenerateDailyNewsReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def manifest(self):
        item = {
            "news_id": "news-1",
            "event_type": "share_repurchase",
            "title": "回购新闻",
            "url": "https://example.test/news",
            "publisher": "测试媒体",
            "published_at": "2026-08-08T10:00:00+08:00",
            "fetched_at": "2026-08-08T10:01:00+08:00",
            "raw_record_id": "raw-1",
            "security_name": "甲公司",
        }
        content = (json.dumps(item, ensure_ascii=False) + "\n").encode()
        (self.root / "news.jsonl").write_bytes(content)
        manifest = {
            "table": {
                "logical_name": "news_items",
                "file": "news.jsonl",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        }
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest))
        return path

    def test_renders_source_and_verification_warning(self):
        content = render_report(load_news([self.manifest()]), "2026-08-08")
        self.assertIn("甲公司", content)
        self.assertIn("同花顺问财", content)
        self.assertIn("交叉验证", content)

    def test_rejects_tampered_file(self):
        manifest = self.manifest()
        (self.root / "news.jsonl").write_text("{}\n")
        with self.assertRaisesRegex(NewsReportError, "hash mismatch"):
            load_news([manifest])


if __name__ == "__main__":
    unittest.main()
