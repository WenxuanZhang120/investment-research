import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.classify_portfolio_review import classify_portfolio  # noqa: E402


class ClassifyPortfolioReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cards = self.root / "cards"
        self.cards.mkdir()

    def write_inputs(self, thesis_status="intact", valuation_status="attractive", risk_status="stable"):
        holdings = self.root / "holdings.csv"
        fields = json.loads(
            (REPOSITORY_ROOT / "config/portfolio_schema.json").read_text()
        )["holdings"]["columns"]
        with holdings.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(
                {
                    "as_of_date": "2026-08-08",
                    "security_code": "600000.SH",
                    "security_name": "甲",
                    "quantity": "1",
                    "average_cost": "1",
                    "currency": "CNY",
                    "market_value": "20",
                    "target_weight": "",
                    "notes": "",
                }
            )
            writer.writerow(
                {
                    "as_of_date": "2026-08-08",
                    "security_code": "000001.SZ",
                    "security_name": "乙",
                    "quantity": "1",
                    "average_cost": "1",
                    "currency": "CNY",
                    "market_value": "80",
                    "target_weight": "",
                    "notes": "",
                }
            )
        template = json.loads(
            (REPOSITORY_ROOT / "portfolio/investment_card.template.json").read_text()
        )
        template.update(
            {
                "security_code": "600000.SH",
                "updated_at": "2026-08-08T10:00:00+08:00",
                "thesis_status": thesis_status,
                "valuation_status": valuation_status,
                "risk_status": risk_status,
                "target_weight": 0.4,
            }
        )
        (self.cards / "600000.SH.json").write_text(json.dumps(template))
        return holdings

    def test_add_candidate_requires_explicit_intact_attractive_state(self):
        result = classify_portfolio(self.write_inputs(), self.cards)
        by_code = {x["security_code"]: x for x in result["records"]}
        self.assertEqual(by_code["600000.SH"]["category"], "ADD_candidate")
        self.assertEqual(by_code["000001.SZ"]["category"], "REVIEW")

    def test_broken_thesis_has_exit_precedence(self):
        result = classify_portfolio(
            self.write_inputs(thesis_status="broken", risk_status="material"),
            self.cards,
        )
        item = next(x for x in result["records"] if x["security_code"] == "600000.SH")
        self.assertEqual(item["category"], "EXIT_candidate")


if __name__ == "__main__":
    unittest.main()
