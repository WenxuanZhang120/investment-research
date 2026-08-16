import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.screen_market_research_queue import (  # noqa: E402
    CONNECTOR_DIRECTORY,
    CONNECTOR_MAX_FILE_BYTES,
    _percentile_scores,
    build_screen,
    write_screen,
)


class ScreenMarketResearchQueueTests(unittest.TestCase):
    def _write_synthetic_inputs(self, root: Path):
        config = root / "config"
        config.mkdir(parents=True)
        rules = config / "screening_rules.json"
        universe = config / "investment_universe.json"
        shutil.copy2(REPOSITORY_ROOT / "config/screening_rules.json", rules)
        shutil.copy2(REPOSITORY_ROOT / "config/investment_universe.json", universe)

        market = root / "data/normalized/runs/test/market"
        market.mkdir(parents=True)
        valuations = [
            {
                "security_code": "000001.SZ",
                "as_of_date": "2026-08-08",
                "fetched_at": "2026-08-08T10:00:00+08:00",
                "pe_ttm": 10.0,
                "market_cap": 100.0,
            },
            {
                "security_code": "000002.SZ",
                "as_of_date": "2026-08-08",
                "fetched_at": "2026-08-08T10:00:00+08:00",
                "pe_ttm": 20.0,
                "market_cap": 200.0,
            },
            {
                "security_code": "600001.SH",
                "as_of_date": "2026-08-08",
                "fetched_at": "2026-08-08T10:00:00+08:00",
                "pe_ttm": 15.0,
                "market_cap": 150.0,
            },
            {
                "security_code": "300001.SZ",
                "as_of_date": "2026-08-08",
                "fetched_at": "2026-08-08T10:00:00+08:00",
                "pe_ttm": 12.0,
                "market_cap": 120.0,
            },
        ]
        valuation_content = (
            "\n".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True)
                for row in valuations
            )
            + "\n"
        ).encode("utf-8")
        (market / "valuation_snapshots.jsonl").write_bytes(valuation_content)

        names = {
            "000001.SZ": "甲公司",
            "000002.SZ": "乙公司",
            "600001.SH": "丙公司",
            "300001.SZ": "创业板公司",
        }
        security_master = [
            {
                "security_code": code,
                "security_name": name,
                "exchange": code[-2:],
                "listing_status": "正常上市",
                "market_memberships": (
                    ["创业板"] if code.startswith("300") else ["沪深A股"]
                ),
                "observed_date": "2026-08-08",
                "fetched_at": "2026-08-08T10:00:00+08:00",
            }
            for code, name in names.items()
        ]
        security_content = (
            "\n".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True)
                for row in security_master
            )
            + "\n"
        ).encode("utf-8")
        (market / "security_master.jsonl").write_bytes(security_content)

        market_manifest = market / "manifest.json"
        market_manifest.write_text(
            json.dumps(
                {
                    "tables": {
                        "security_master": {
                            "file": "security_master.jsonl",
                            "record_count": len(security_master),
                            "sha256": hashlib.sha256(security_content).hexdigest(),
                        },
                        "valuation_snapshots": {
                            "file": "valuation_snapshots.jsonl",
                            "record_count": len(valuations),
                            "sha256": hashlib.sha256(valuation_content).hexdigest(),
                        },
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        metrics = root / "data/derived/runs/test/metrics"
        metrics.mkdir(parents=True)
        metric_rows = []
        values = {
            "000001.SZ": (0.20, 0.30),
            "000002.SZ": (0.25, 0.20),
            "600001.SH": (0.30, 0.25),
            "300001.SZ": (0.35, 0.35),
        }
        for code, (profit_margin, cash_flow_margin) in values.items():
            for metric_name, value in (
                ("net_profit_margin", profit_margin),
                ("operating_cash_flow_margin", cash_flow_margin),
            ):
                metric_rows.append(
                    {
                        "security_code": code,
                        "security_name": names[code],
                        "metric_name": metric_name,
                        "value": value,
                        "period_end": "2025-12-31",
                        "available_from": (
                            "2026-08-09"
                            if code == "600001.SH"
                            and metric_name == "net_profit_margin"
                            else "2026-04-01"
                        ),
                        "fetched_at": "2026-08-08T10:00:00+08:00",
                        "calculation_status": "calculated",
                    }
                )
        metric_content = (
            "\n".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True)
                for row in metric_rows
            )
            + "\n"
        ).encode("utf-8")
        (metrics / "financial_metrics.jsonl").write_bytes(metric_content)
        metric_manifest = metrics / "manifest.json"
        metric_manifest.write_text(
            json.dumps(
                {
                    "table": {
                        "logical_name": "financial_metrics",
                        "record_count": len(metric_rows),
                        "partitions": [
                            {
                                "file": "financial_metrics.jsonl",
                                "record_count": len(metric_rows),
                                "sha256": hashlib.sha256(metric_content).hexdigest(),
                            }
                        ],
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return market_manifest, metric_manifest, rules, universe

    def _build_synthetic_screen(self, root: Path):
        market, metrics, rules, universe = self._write_synthetic_inputs(root)
        return build_screen(
            market,
            metrics,
            rules_path=rules,
            universe_path=universe,
        )

    def test_percentile_direction_is_explicit(self):
        values = {"A": 1.0, "B": 2.0, "C": 3.0}
        self.assertEqual(_percentile_scores(values, "higher")["C"], 1.0)
        self.assertEqual(_percentile_scores(values, "lower")["A"], 1.0)

    def test_synthetic_inputs_build_point_in_time_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            built = self._build_synthetic_screen(Path(temporary))
            self.assertEqual(built["coverage"]["universe_count"], 3)
            self.assertEqual(
                sum(built["coverage"]["priority_counts"].values()), 3
            )
            self.assertEqual(built["coverage"]["excluded_by_universe_count"], 1)
            self.assertEqual(
                {record["security_code"] for record in built["records"]},
                {"000001.SZ", "000002.SZ", "600001.SH"},
            )
            eligible = [record for record in built["records"] if record["eligible"]]
            self.assertEqual(
                {record["security_code"] for record in eligible},
                {"000001.SZ", "000002.SZ"},
            )
            self.assertTrue(
                all(
                    record["financial_available_from"] <= record["as_of_date"]
                    for record in eligible
                )
            )
            late = next(
                record
                for record in built["records"]
                if record["security_code"] == "600001.SH"
            )
            self.assertFalse(late["eligible"])
            self.assertIn(
                "not_available_at_screen_date:net_profit_margin",
                late["eligibility_reasons"],
            )

    def test_writes_hashed_immutable_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            built = self._build_synthetic_screen(root)
            destination = write_screen(
                built,
                derived_root=root / "data/derived",
                repository_root=root,
            )
            manifest = json.loads((destination / "manifest.json").read_text())
            content = (destination / manifest["table"]["file"]).read_bytes()
            self.assertEqual(manifest["table"]["sha256"], hashlib.sha256(content).hexdigest())
            connector = destination / CONNECTOR_DIRECTORY
            connector_manifest = json.loads(
                (connector / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(connector_manifest["source_manifest"], "../manifest.json")
            self.assertEqual(
                connector_manifest["source_table"]["sha256"],
                manifest["table"]["sha256"],
            )
            summary = connector_manifest["tables"]["p0_p1_summary"]
            summary_content = (connector / summary["file"]).read_bytes()
            self.assertLessEqual(len(summary_content), CONNECTOR_MAX_FILE_BYTES)
            self.assertEqual(summary["sha256"], hashlib.sha256(summary_content).hexdigest())
            self.assertTrue(
                all(
                    json.loads(line)["priority"] in {"P0", "P1"}
                    for line in summary_content.decode("utf-8").splitlines()
                )
            )
            partition_content = b""
            for partition in connector_manifest["tables"]["full_queue"]["partitions"]:
                chunk = (connector / partition["file"]).read_bytes()
                self.assertLessEqual(len(chunk), CONNECTOR_MAX_FILE_BYTES)
                self.assertEqual(partition["sha256"], hashlib.sha256(chunk).hexdigest())
                partition_content += chunk
            self.assertEqual(partition_content, content)
            with self.assertRaises(FileExistsError):
                write_screen(
                    built,
                    derived_root=root / "data/derived",
                    repository_root=root,
                )


if __name__ == "__main__":
    unittest.main()
