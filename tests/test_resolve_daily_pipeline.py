import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.normalize_iwencai_financials import NORMALIZER_VERSION  # noqa: E402
from scripts.resolve_daily_pipeline import (  # noqa: E402
    BASIC_CALCULATOR_VERSION,
    ReadinessError,
    resolve_pipeline_config,
)
from scripts.save_raw_response import save_raw_response  # noqa: E402


class ResolveDailyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for directory in (
            "config",
            "data/raw",
            "data/normalized",
            "data/derived",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.job = {
            "job_id": "complete_base",
            "period_end": "2025-12-31",
            "purpose": "test_complete_input",
            "query": "全部A股2025年年报测试问句",
        }
        (self.root / "config/financial_collection_plan.json").write_text(
            json.dumps(
                {
                    "plan_version": "test",
                    "page_limit": 100,
                    "jobs": [self.job],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "config/field_mappings.json").write_text(
            json.dumps({"mapping_version": "test-map"}), encoding="utf-8"
        )
        (self.root / "config/financial_metrics.json").write_text(
            json.dumps({"metric_definition_version": "test-basic"}),
            encoding="utf-8",
        )
        (self.root / "config/advanced_financial_metrics.json").write_text(
            json.dumps({"metric_definition_version": "test-advanced"}),
            encoding="utf-8",
        )

    def config(self):
        return {
            "pipeline_version": "test",
            "purpose": "test",
            "llm_calls_allowed": False,
            "external_collection_enabled": False,
            "input_readiness": {
                "schema_version": 1,
                "financial_collection_plan": "config/financial_collection_plan.json",
                "raw_root": "data/raw",
                "mapping_file": "config/field_mappings.json",
                "normalized_root": "data/normalized",
                "derived_root": "data/derived",
                "basic_metric_config": "config/financial_metrics.json",
                "advanced_metric_config": "config/advanced_financial_metrics.json",
                "normalization_job_ids": ["complete_base"],
                "basic_metric_job_ids": ["complete_base"],
                "advanced_metric_job_ids": [],
                "timeout_seconds": 30,
            },
            "stages": [
                {"stage": "status", "steps": []},
                {"stage": "normalization", "steps": []},
                {"stage": "derivation", "steps": []},
                {"stage": "reporting", "steps": []},
                {"stage": "validation", "steps": []},
            ],
        }

    def save_complete_raw_input(self):
        return save_raw_response(
            {
                "success": True,
                "query": self.job["query"],
                "code_count": 1,
                "returned_count": 1,
                "page": "1",
                "limit": "100",
                "has_more": False,
                "datas": [{"股票代码": "000001.SZ"}],
            },
            source="iwencai",
            query=self.job["query"],
            raw_root=self.root / "data/raw",
            fetched_at=datetime(
                2026, 8, 9, 10, 0, tzinfo=timezone(timedelta(hours=8))
            ),
        )

    def test_complete_input_plans_once_then_becomes_up_to_date(self):
        snapshot = self.save_complete_raw_input()
        first = resolve_pipeline_config(self.config(), repository_root=self.root)
        self.assertEqual(first["readiness"]["status"], "work_planned")
        self.assertEqual(first["readiness"]["planned_step_count"], 2)
        self.assertEqual(len(first["stages"][1]["steps"]), 1)
        self.assertEqual(len(first["stages"][2]["steps"]), 1)
        normalization_command = first["stages"][1]["steps"][0]["command"]
        self.assertIn("config/financial_collection_plan.json", normalization_command)
        self.assertIn("data/raw", normalization_command)
        self.assertIn("data/normalized", normalization_command)
        derivation_command = first["stages"][2]["steps"][0]["command"]
        self.assertIn("config/financial_metrics.json", derivation_command)
        self.assertIn("data/derived", derivation_command)
        serialized = json.dumps(first)
        self.assertNotIn(str(self.root), serialized)

        manifest_relative = first["readiness"]["basic_financial_metrics"][0][
            "source_manifest"
        ]
        manifest = self.root / manifest_relative
        manifest.parent.mkdir(parents=True)
        record_id = json.loads(snapshot.read_text(encoding="utf-8"))["metadata"][
            "record_id"
        ]
        manifest.write_text(
            json.dumps(
                {
                    "bundle_id": manifest.parent.name,
                    "normalizer_version": NORMALIZER_VERSION,
                    "mapping_version": "test-map",
                    "raw_records": [{"record_id": record_id}],
                }
            ),
            encoding="utf-8",
        )

        second = resolve_pipeline_config(self.config(), repository_root=self.root)
        self.assertEqual(second["stages"][1]["steps"], [])
        self.assertEqual(len(second["stages"][2]["steps"]), 1)
        self.assertEqual(
            second["readiness"]["financial_jobs"][0]["normalization_status"],
            "up_to_date",
        )

        derived = (
            self.root
            / "data/derived/runs/iwencai/2026/08/09/test-derived/manifest.json"
        )
        derived.parent.mkdir(parents=True)
        derived.write_text(
            json.dumps(
                {
                    "source_financial_bundle_id": manifest.parent.name,
                    "source_financial_manifest": manifest_relative,
                    "calculator_version": BASIC_CALCULATOR_VERSION,
                    "metric_definition_version": "test-basic",
                    "table": {"logical_name": "financial_metrics"},
                }
            ),
            encoding="utf-8",
        )
        third = resolve_pipeline_config(self.config(), repository_root=self.root)
        self.assertEqual(third["readiness"]["status"], "up_to_date")
        self.assertEqual(third["readiness"]["planned_step_count"], 0)
        self.assertEqual(third["stages"][1]["steps"], [])
        self.assertEqual(third["stages"][2]["steps"], [])

    def test_incomplete_input_is_explicitly_skipped(self):
        resolved = resolve_pipeline_config(self.config(), repository_root=self.root)
        self.assertEqual(
            resolved["readiness"]["status"], "waiting_for_complete_input"
        )
        self.assertEqual(resolved["readiness"]["planned_step_count"], 0)
        self.assertEqual(resolved["stages"][1]["steps"], [])
        self.assertEqual(resolved["stages"][2]["steps"], [])

    def test_advanced_metrics_are_planned_only_after_configured_input_is_complete(self):
        self.save_complete_raw_input()
        config = self.config()
        config["input_readiness"]["basic_metric_job_ids"] = []
        config["input_readiness"]["advanced_metric_job_ids"] = ["complete_base"]
        resolved = resolve_pipeline_config(config, repository_root=self.root)
        self.assertEqual(len(resolved["stages"][1]["steps"]), 1)
        self.assertEqual(len(resolved["stages"][2]["steps"]), 1)
        command = resolved["stages"][2]["steps"][0]["command"]
        self.assertEqual(command[0], "scripts/derive_advanced_financial_metrics.py")
        self.assertIn("config/advanced_financial_metrics.json", command)
        self.assertIn("data/derived", command)
        self.assertNotIn(str(self.root), json.dumps(resolved))

    def test_outside_repository_configuration_is_rejected(self):
        config = self.config()
        config["input_readiness"]["financial_collection_plan"] = "../plan.json"
        with self.assertRaisesRegex(ReadinessError, "outside repository root"):
            resolve_pipeline_config(config, repository_root=self.root)

    def test_unknown_job_is_rejected(self):
        config = self.config()
        config["input_readiness"]["normalization_job_ids"] = ["unknown"]
        config["input_readiness"]["basic_metric_job_ids"] = []
        with self.assertRaisesRegex(ReadinessError, "unknown readiness job ids"):
            resolve_pipeline_config(config, repository_root=self.root)


if __name__ == "__main__":
    unittest.main()
