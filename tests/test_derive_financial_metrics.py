import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.derive_financial_metrics import (  # noqa: E402
    DerivationError,
    _calculate_metric,
    build_derived_metrics,
    derive_financial_metrics,
)


SAMPLE_MANIFEST = REPOSITORY_ROOT / (
    "data/normalized/runs/iwencai/2026/08/08/"
    "0034a5c33c31a70e5124/manifest.json"
)


class DeriveFinancialMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_builds_five_metrics_for_each_sample_report(self) -> None:
        built = build_derived_metrics(SAMPLE_MANIFEST)

        self.assertEqual(built["coverage"]["security_count"], 3)
        self.assertEqual(built["coverage"]["period_ends"], ["2025-12-31", "2026-03-31"])
        self.assertEqual(built["coverage"]["metric_count"], 5)
        self.assertEqual(built["coverage"]["record_count"], 30)
        self.assertEqual(built["coverage"]["calculated_count"], 30)
        metric = next(
            record
            for record in built["records"]
            if record["security_code"] == "600519.SH"
            and record["period_end"] == "2025-12-31"
            and record["metric_name"] == "net_profit_margin"
        )
        self.assertEqual(metric["unit"], "ratio")
        self.assertEqual(metric["calculation_status"], "calculated")
        self.assertFalse(metric["annualized"])
        self.assertEqual(len(metric["input_facts"]), 2)

    def test_denominator_rules_are_explicit(self) -> None:
        self.assertEqual(_calculate_metric(1, 0, "nonzero"), (None, "zero_denominator"))
        self.assertEqual(
            _calculate_metric(1, -1, "positive"),
            (None, "non_positive_denominator"),
        )
        self.assertEqual(_calculate_metric(None, 1, "nonzero"), (None, "missing_inputs"))

    def test_writes_period_partitions_and_refuses_overwrite(self) -> None:
        destination = derive_financial_metrics(
            SAMPLE_MANIFEST,
            derived_root=self.root,
        )

        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["coverage"]["record_count"], 30)
        self.assertEqual(len(manifest["table"]["partitions"]), 2)
        with self.assertRaises(FileExistsError):
            derive_financial_metrics(SAMPLE_MANIFEST, derived_root=self.root)

    def test_rejects_tampered_fact_partition(self) -> None:
        source_dir = SAMPLE_MANIFEST.parent
        copied = self.root / "source"
        copied.mkdir()
        manifest = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
        for table in manifest["tables"].values():
            filename = table.get("file")
            if filename:
                (copied / filename).write_bytes((source_dir / filename).read_bytes())
        fact_path = copied / manifest["tables"]["financial_facts"]["file"]
        with fact_path.open("ab") as handle:
            handle.write(b"{}\n")
        copied_manifest = copied / "manifest.json"
        copied_manifest.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(DerivationError, "hash mismatch"):
            build_derived_metrics(copied_manifest)


if __name__ == "__main__":
    unittest.main()
