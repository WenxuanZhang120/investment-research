import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.normalize_iwencai_announcements import build_events, write_bundle  # noqa: E402
from scripts.publish_daily_monitoring_report import (  # noqa: E402
    build_monitoring_report,
    write_monitoring_report,
)
from scripts.resolve_research_pipeline import resolve_research_pipeline  # noqa: E402
from scripts.save_raw_response import save_raw_response  # noqa: E402
from scripts.screen_market_research_queue import build_screen, write_screen  # noqa: E402


class ResolveResearchPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for directory in (
            "config",
            "data/raw",
            "data/normalized",
            "data/derived",
            "reports/daily",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            REPOSITORY_ROOT / "config/event_taxonomy.json",
            self.root / "config/event_taxonomy.json",
        )
        shutil.copy2(
            REPOSITORY_ROOT / "config/screening_rules.json",
            self.root / "config/screening_rules.json",
        )
        self.query = "A股 最近七日 股份回购公告"

    def settings(self, *, minimum=1):
        return {
            "schema_version": 1,
            "raw_root": "data/raw",
            "normalized_root": "data/normalized",
            "derived_root": "data/derived",
            "reports_root": "reports/daily",
            "event_taxonomy": "config/event_taxonomy.json",
            "screening_rules": "config/screening_rules.json",
            "minimum_screening_universe": minimum,
            "timeout_seconds": 30,
            "monitoring_routes": [
                {
                    "route_id": "announcements",
                    "stream": "announcements",
                    "query": self.query,
                }
            ],
        }

    def save_announcement(self):
        return save_raw_response(
            {
                "status_code": 0,
                "data": [
                    {
                        "title": "甲公司关于股份回购的公告",
                        "url": "https://example.test/a",
                        "publish_time": "2026-08-08T09:30:00+08:00",
                    }
                ],
            },
            source="iwencai",
            query=self.query,
            raw_root=self.root / "data/raw",
            fetched_at=datetime(
                2026, 8, 8, 10, 0, tzinfo=timezone(timedelta(hours=8))
            ),
        )

    def test_monitoring_is_planned_once_then_becomes_up_to_date(self):
        snapshot = self.save_announcement()
        first = resolve_research_pipeline(
            self.settings(), repository_root=self.root
        )
        self.assertEqual(len(first["normalization_steps"]), 1)
        self.assertEqual(len(first["reporting_steps"]), 1)
        self.assertNotIn(str(self.root), json.dumps(first))

        normalized = write_bundle(
            build_events(
                snapshot,
                taxonomy_path=self.root / "config/event_taxonomy.json",
                repository_root=self.root,
            ),
            normalized_root=self.root / "data/normalized",
        )
        report = build_monitoring_report(
            "events",
            [normalized / "manifest.json"],
            report_date="2026-08-08",
            repository_root=self.root,
        )
        write_monitoring_report(
            report,
            reports_root=self.root / "reports/daily",
            repository_root=self.root,
        )
        second = resolve_research_pipeline(
            self.settings(), repository_root=self.root
        )
        self.assertEqual(second["normalization_steps"], [])
        self.assertEqual(second["reporting_steps"], [])
        route = second["monitoring"]["routes"][0]
        self.assertEqual(route["snapshots"][0]["normalization_status"], "up_to_date")
        self.assertEqual(route["reports"][0]["status"], "up_to_date")

    def write_screening_inputs(self):
        market_dir = self.root / "data/normalized/runs/test/market"
        market_dir.mkdir(parents=True)
        valuations = (
            json.dumps(
                {
                    "security_code": "000001.SZ",
                    "as_of_date": "2026-08-07",
                    "fetched_at": "2026-08-08T10:00:00+08:00",
                    "pe_ttm": 10.0,
                    "market_cap": 100.0,
                }
            )
            + "\n"
        ).encode()
        (market_dir / "valuations.jsonl").write_bytes(valuations)
        market_manifest = market_dir / "manifest.json"
        market_manifest.write_text(
            json.dumps(
                {
                    "tables": {
                        "valuation_snapshots": {
                            "file": "valuations.jsonl",
                            "record_count": 1,
                            "sha256": hashlib.sha256(valuations).hexdigest(),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        metric_dir = self.root / "data/derived/runs/test/metrics"
        metric_dir.mkdir(parents=True)
        rows = []
        for name, value in (
            ("net_profit_margin", 0.2),
            ("operating_cash_flow_margin", 0.3),
        ):
            rows.append(
                {
                    "security_code": "000001.SZ",
                    "security_name": "甲公司",
                    "metric_name": name,
                    "value": value,
                    "period_end": "2025-12-31",
                    "available_from": "2026-04-01",
                    "fetched_at": "2026-08-08T10:00:00+08:00",
                    "calculation_status": "calculated",
                }
            )
        metrics = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
        (metric_dir / "metrics.jsonl").write_bytes(metrics)
        metric_manifest = metric_dir / "manifest.json"
        metric_manifest.write_text(
            json.dumps(
                {
                    "coverage": {"security_count": 1},
                    "table": {
                        "logical_name": "financial_metrics",
                        "partitions": [
                            {
                                "file": "metrics.jsonl",
                                "period_end": "2025-12-31",
                                "sha256": hashlib.sha256(metrics).hexdigest(),
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        return market_manifest, metric_manifest

    def test_full_point_in_time_inputs_plan_screen_once(self):
        market, metrics = self.write_screening_inputs()
        first = resolve_research_pipeline(
            self.settings(), repository_root=self.root
        )
        self.assertEqual(first["screening"]["status"], "planned")
        self.assertEqual(len(first["derivation_steps"]), 1)
        self.assertNotIn(str(self.root), json.dumps(first))

        destination = write_screen(
            build_screen(
                market,
                metrics,
                rules_path=self.root / "config/screening_rules.json",
            ),
            derived_root=self.root / "data/derived",
            repository_root=self.root,
        )
        second = resolve_research_pipeline(
            self.settings(), repository_root=self.root
        )
        self.assertEqual(second["screening"]["status"], "up_to_date")
        self.assertEqual(second["derivation_steps"], [])
        self.assertEqual(
            second["screening"]["derived_manifest"],
            (destination / "manifest.json").relative_to(self.root).as_posix(),
        )


if __name__ == "__main__":
    unittest.main()
