import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.validate_portfolio import (  # noqa: E402
    _schema,
    validate_card,
    validate_csv,
)


class ValidatePortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.schema = _schema(REPOSITORY_ROOT / "config" / "portfolio_schema.json")

    def write_csv(self, name, definition, row):
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=definition["columns"])
            writer.writeheader()
            writer.writerow(row)
        return path

    def test_validates_holdings_without_calculation(self):
        definition = self.schema["holdings"]
        path = self.write_csv(
            "holdings.csv",
            definition,
            {
                "as_of_date": "2026-08-08",
                "security_code": "600000.SH",
                "security_name": "",
                "quantity": "1",
                "average_cost": "10",
                "currency": "CNY",
                "market_value": "",
                "target_weight": "0.1",
                "notes": "",
            },
        )
        self.assertEqual(validate_csv(path, definition), [])

    def test_rejects_negative_number_and_bad_ratio(self):
        definition = self.schema["holdings"]
        path = self.write_csv(
            "holdings.csv",
            definition,
            {
                "as_of_date": "bad",
                "security_code": "600000.SH",
                "quantity": "-1",
                "average_cost": "10",
                "currency": "CNY",
                "target_weight": "2",
            },
        )
        errors = validate_csv(path, definition)
        self.assertEqual(len(errors), 3)
        self.assertTrue(all("600000" not in error for error in errors))

    def test_validates_investment_card_structure(self):
        card = json.loads(
            (REPOSITORY_ROOT / "portfolio" / "investment_card.template.json").read_text(
                encoding="utf-8"
            )
        )
        card.update(
            {
                "security_code": "600000.SH",
                "updated_at": "2026-08-08T10:00:00+08:00",
                "thesis": "test",
                "why_now": "test",
                "valuation_framework": "test",
            }
        )
        path = self.root / "card.json"
        path.write_text(json.dumps(card), encoding="utf-8")
        self.assertEqual(validate_card(path, self.schema["investment_card"]), [])


if __name__ == "__main__":
    unittest.main()
