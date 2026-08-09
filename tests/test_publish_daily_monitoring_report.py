import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.normalize_iwencai_announcements import build_events, write_bundle  # noqa: E402
from scripts.publish_daily_monitoring_report import (  # noqa: E402
    MonitoringReportError,
    build_monitoring_report,
    write_monitoring_report,
)
from scripts.save_raw_response import save_raw_response  # noqa: E402


class PublishDailyMonitoringReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.raw = save_raw_response(
            {
                "status_code": 0,
                "data": [
                    {
                        "title": "甲公司关于股份回购的公告",
                        "url": "https://example.test/announcement",
                        "publish_time": "2026-08-08T09:30:00+08:00",
                        "stock_infos": [{"code": "600001", "name": "甲公司"}],
                    }
                ],
            },
            source="iwencai",
            query="A股 最近七日 股份回购公告",
            raw_root=self.root / "data/raw",
            fetched_at=datetime(
                2026, 8, 8, 10, 0, tzinfo=timezone(timedelta(hours=8))
            ),
        )
        built = build_events(self.raw, repository_root=self.root)
        bundle = write_bundle(
            built, normalized_root=self.root / "data/normalized"
        )
        self.source_manifest = bundle / "manifest.json"

    def build(self):
        return build_monitoring_report(
            "events",
            [self.source_manifest],
            report_date="2026-08-08",
            repository_root=self.root,
        )

    def test_writes_portable_immutable_idempotent_report_bundle(self):
        first = write_monitoring_report(
            self.build(),
            reports_root=self.root / "reports/daily",
            repository_root=self.root,
        )
        manifest = json.loads(first.read_text(encoding="utf-8"))
        self.assertEqual(manifest["kind"], "events")
        self.assertEqual(manifest["coverage"]["record_count"], 1)
        self.assertFalse(manifest["investment_judgment_included"])
        self.assertFalse(manifest["automatic_trading_enabled"])
        serialized = first.read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), serialized)
        self.assertEqual(
            manifest["source_manifests"][0]["path"],
            self.source_manifest.relative_to(self.root).as_posix(),
        )

        second = write_monitoring_report(
            self.build(),
            reports_root=self.root / "reports/daily",
            repository_root=self.root,
        )
        self.assertEqual(second, first)

    def test_rejects_existing_report_with_different_content(self):
        destination = write_monitoring_report(
            self.build(),
            reports_root=self.root / "reports/daily",
            repository_root=self.root,
        )
        report_name = json.loads(destination.read_text(encoding="utf-8"))["report"][
            "file"
        ]
        (destination.parent / report_name).write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(MonitoringReportError, "differs"):
            write_monitoring_report(
                self.build(),
                reports_root=self.root / "reports/daily",
                repository_root=self.root,
            )

    def test_rejects_source_manifest_outside_repository(self):
        with self.assertRaisesRegex(ValueError, "outside repository root"):
            build_monitoring_report(
                "events",
                [REPOSITORY_ROOT / "data/normalized/README.md"],
                report_date="2026-08-08",
                repository_root=self.root,
            )


if __name__ == "__main__":
    unittest.main()
