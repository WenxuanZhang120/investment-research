import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.normalize_iwencai_announcements import (  # noqa: E402
    AnnouncementNormalizationError,
    build_events,
    write_bundle,
)
from scripts.save_raw_response import save_raw_response  # noqa: E402


class NormalizeIwencaiAnnouncementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fetched_at = datetime(
            2026, 8, 8, 21, 0, tzinfo=timezone(timedelta(hours=8))
        )

    def snapshot(self):
        payload = {
            "status_code": 0,
            "data": [
                {
                    "title": "甲公司关于股份回购的公告",
                    "summary": "摘要",
                    "url": "https://example.test/a",
                    "publish_time": 1786118400000,
                    "stock_infos": [{"code": "600001", "name": "甲公司"}],
                },
                {
                    "title": "乙公司2025年年度报告",
                    "summary": "",
                    "url": "https://example.test/b",
                    "publish_time": "2026-08-08T09:30:00+08:00",
                },
            ],
        }
        return save_raw_response(
            payload,
            source="iwencai",
            query="A股 最新公告",
            raw_root=self.root / "raw",
            fetched_at=self.fetched_at,
        )

    def test_builds_auditable_classified_events(self):
        built = build_events(self.snapshot())
        self.assertEqual(len(built["records"]), 2)
        self.assertEqual(
            {x["event_type"] for x in built["records"]},
            {"share_repurchase", "periodic_report"},
        )
        self.assertTrue(all(x["available_from"] == x["published_at"] for x in built["records"]))
        self.assertTrue(all(x["raw_item"] for x in built["records"]))
        repurchase = next(x for x in built["records"] if x["event_type"] == "share_repurchase")
        self.assertEqual(repurchase["source_security_code"], "600001")
        self.assertIsNone(repurchase["security_code"])

    def test_writes_hashed_immutable_bundle(self):
        built = build_events(self.snapshot())
        destination = write_bundle(built, normalized_root=self.root / "normalized")
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        content = (destination / "events.jsonl").read_bytes()
        self.assertEqual(manifest["table"]["record_count"], 2)
        self.assertEqual(manifest["table"]["sha256"], hashlib.sha256(content).hexdigest())
        with self.assertRaises(FileExistsError):
            write_bundle(built, normalized_root=self.root / "normalized")

    def test_rejects_tampered_snapshot(self):
        path = self.snapshot()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["payload"]["data"][0]["title"] = "tampered"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(AnnouncementNormalizationError, "checksum"):
            build_events(path)


if __name__ == "__main__":
    unittest.main()
