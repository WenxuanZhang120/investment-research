import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.normalize_iwencai_news import build_news, write_bundle  # noqa: E402
from scripts.save_raw_response import save_raw_response  # noqa: E402


class NormalizeIwencaiNewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def snapshot(self):
        return save_raw_response(
            {
                "status_code": 0,
                "data": [
                    {
                        "title": "甲公司发布股份回购进展",
                        "summary": "摘要",
                        "url": "https://example.test/news",
                        "publish_time": 1786118400,
                        "source_original": "这是新闻正文，不是媒体名称",
                        "extra": {
                            "publish_source": "转载平台",
                            "real_publish_source": "测试媒体"
                        },
                        "stock_infos": [{"code": "600001", "name": "甲公司"}],
                    }
                ],
            },
            source="iwencai",
            query="甲公司最新新闻",
            raw_root=self.root / "raw",
            fetched_at=datetime(
                2026, 8, 8, 10, tzinfo=timezone(timedelta(hours=8))
            ),
        )

    def test_builds_point_in_time_news_with_raw_lineage(self):
        built = build_news(self.snapshot(), repository_root=self.root)
        item = built["records"][0]
        self.assertEqual(item["event_type"], "share_repurchase")
        self.assertEqual(item["publisher"], "测试媒体")
        self.assertEqual(item["source_security_code"], "600001")
        self.assertEqual(item["available_from"], item["published_at"])
        self.assertTrue(item["raw_item"])

    def test_writes_hashed_immutable_bundle(self):
        built = build_news(self.snapshot(), repository_root=self.root)
        destination = write_bundle(built, normalized_root=self.root / "normalized")
        manifest = json.loads((destination / "manifest.json").read_text())
        content = (destination / "news_items.jsonl").read_bytes()
        self.assertEqual(manifest["table"]["sha256"], hashlib.sha256(content).hexdigest())
        with self.assertRaises(FileExistsError):
            write_bundle(built, normalized_root=self.root / "normalized")

    def test_p0_scope_filters_news_for_non_target_companies(self):
        path = save_raw_response(
            {
                "status_code": 0,
                "data": [
                    {
                        "title": "甲公司重要新闻",
                        "url": "https://example.test/p0-news",
                        "publish_time": "2026-08-08T09:30:00+08:00",
                        "stock_infos": [{"code": "600001", "name": "甲公司"}],
                    },
                    {
                        "title": "乙公司重要新闻",
                        "url": "https://example.test/non-p0-news",
                        "publish_time": "2026-08-08T09:31:00+08:00",
                        "stock_infos": [{"code": "600002", "name": "乙公司"}],
                    },
                ],
            },
            source="iwencai",
            query="P0公司新闻",
            raw_root=self.root / "raw",
            fetched_at=datetime(
                2026, 8, 8, 10, tzinfo=timezone(timedelta(hours=8))
            ),
            collection_scope={
                "scope_schema_version": 1,
                "scope_type": "p0_securities",
                "priority": "P0",
                "target_source_manifest": "data/derived/example/manifest.json",
                "target_as_of_date": "2026-08-07",
                "target_security_codes": ["600001.SH"],
            },
        )
        built = build_news(path, repository_root=self.root)
        self.assertEqual(len(built["records"]), 1)
        self.assertEqual(built["records"][0]["source_security_code"], "600001")


if __name__ == "__main__":
    unittest.main()
