import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.generate_daily_event_report import (  # noqa: E402
    EventReportError,
    load_events,
    render_report,
)


class GenerateDailyEventReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def manifest(self):
        event = {
            "event_id": "event-1",
            "event_type": "share_repurchase",
            "title": "股份回购公告",
            "url": "https://example.test/a",
            "published_at": "2026-08-08T10:00:00+08:00",
            "fetched_at": "2026-08-08T10:01:00+08:00",
            "raw_record_id": "raw-1",
            "security_name": "甲公司",
            "source_security_code": "600001",
        }
        content = (json.dumps(event, ensure_ascii=False) + "\n").encode()
        (self.root / "events.jsonl").write_bytes(content)
        manifest = {
            "table": {
                "logical_name": "events",
                "file": "events.jsonl",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        }
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_loads_verified_events_and_renders_source(self):
        content = render_report(load_events([self.manifest()]), "2026-08-08")
        self.assertIn("甲公司｜股份回购", content)
        self.assertIn("同花顺问财", content)
        self.assertIn("不构成", content)

    def test_rejects_tampered_event_file(self):
        manifest = self.manifest()
        (self.root / "events.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(EventReportError, "hash mismatch"):
            load_events([manifest])


if __name__ == "__main__":
    unittest.main()
