import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.derive_advanced_financial_metrics import (  # noqa: E402
    calculate_records,
    write_bundle,
)


class DeriveAdvancedFinancialMetricsTests(unittest.TestCase):
    @staticmethod
    def fact(value, period, field):
        return {
            "security_code": "600000.SH",
            "security_name": "测试公司",
            "period_end": period,
            "canonical_field_name": field,
            "value": value,
            "value_status": "present",
            "available_from": "2026-04-01" if period == "2025-12-31" else "2025-04-01",
            "fetched_at": "2026-08-08T10:00:00+08:00",
            "raw_record_id": "raw",
        }

    def facts(self):
        values = {
            "2024-12-31": {
                "revenue": 100,
                "net_income_parent": 10,
                "equity_parent": 100,
                "total_equity": 110,
                "monetary_funds": 10,
                "short_term_borrowings": 1,
                "non_current_liabilities_due_within_one_year": 1,
                "long_term_borrowings": 1,
                "bonds_payable": 1,
                "operating_profit": 15,
                "income_tax_expense": 2,
                "total_profit": 12,
                "net_cash_flow_operating": 20,
                "capital_expenditure_cash": 8,
            },
            "2025-12-31": {
                "revenue": 200,
                "net_income_parent": 20,
                "equity_parent": 120,
                "total_equity": 130,
                "monetary_funds": 10,
                "short_term_borrowings": 1,
                "non_current_liabilities_due_within_one_year": 1,
                "long_term_borrowings": 1,
                "bonds_payable": 1,
                "operating_profit": 30,
                "income_tax_expense": 5,
                "total_profit": 25,
                "net_cash_flow_operating": 50,
                "capital_expenditure_cash": 20,
            },
        }
        return {
            ("600000.SH", period, field): self.fact(value, period, field)
            for period, fields in values.items()
            for field, value in fields.items()
        }

    def test_calculates_growth_average_roe_roic_and_fcf(self):
        records = calculate_records(
            self.facts(),
            source_bundle_ids=["a", "b"],
            definition_version="1.0.0",
        )
        current = {
            row["metric_name"]: row
            for row in records
            if row["period_end"] == "2025-12-31"
        }
        self.assertEqual(current["revenue_growth_yoy"]["value"], 1.0)
        self.assertEqual(current["net_income_growth_yoy"]["value"], 1.0)
        self.assertAlmostEqual(current["roe_parent_average"]["value"], 20 / 110)
        self.assertAlmostEqual(current["roic_average"]["value"], 24 / 114)
        self.assertEqual(current["free_cash_flow"]["value"], 30)
        self.assertTrue(all(row["calculation_status"] == "calculated" for row in current.values()))

    def test_missing_prior_period_is_explicit(self):
        latest = {
            key: fact
            for key, fact in self.facts().items()
            if key[1] == "2025-12-31"
        }
        records = calculate_records(
            latest, source_bundle_ids=["a"], definition_version="1.0.0"
        )
        by_name = {row["metric_name"]: row for row in records}
        self.assertEqual(by_name["revenue_growth_yoy"]["calculation_status"], "missing_prior_inputs")
        self.assertEqual(by_name["free_cash_flow"]["calculation_status"], "calculated")

    def test_writes_hashed_immutable_bundle(self):
        records = calculate_records(
            self.facts(), source_bundle_ids=["a"], definition_version="1.0.0"
        )
        built = {
            "bundle_id": "test-bundle",
            "records": records,
            "source_bundle_ids": ["a"],
            "source_manifest_paths": ["a/manifest.json"],
            "fetched_at_start": "2026-08-08T10:00:00+08:00",
            "definition_version": "1.0.0",
        }
        with tempfile.TemporaryDirectory() as temporary:
            destination = write_bundle(built, derived_root=Path(temporary))
            manifest = json.loads((destination / "manifest.json").read_text())
            content = (destination / manifest["table"]["file"]).read_bytes()
            self.assertEqual(manifest["table"]["sha256"], hashlib.sha256(content).hexdigest())
            with self.assertRaises(FileExistsError):
                write_bundle(built, derived_root=Path(temporary))


if __name__ == "__main__":
    unittest.main()
