import json
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.audit_system_completion import audit_system  # noqa: E402


class AuditSystemCompletionTests(unittest.TestCase):
    def test_audit_covers_every_versioned_requirement(self):
        requirements = json.loads(
            (REPOSITORY_ROOT / "config/system_completion_requirements.json").read_text()
        )
        result = audit_system(REPOSITORY_ROOT)
        names = [item["requirement"] for item in result["results"]]
        self.assertEqual(names, requirements["requirements"])
        self.assertEqual(result["complete"], all(x["achieved"] for x in result["results"]))

    def test_any_current_financial_gaps_are_specific(self):
        result = audit_system(REPOSITORY_ROOT)
        by_name = {item["requirement"]: item for item in result["results"]}
        financial = by_name["complete_financial_database"]
        if not financial["achieved"]:
            self.assertTrue(financial["gaps"])
            self.assertTrue(all("period missing" in gap for gap in financial["gaps"]))
        advanced = by_name["advanced_financial_metrics"]
        if not advanced["achieved"]:
            self.assertTrue(advanced["gaps"])
            self.assertTrue(all("period missing" in gap for gap in advanced["gaps"]))


if __name__ == "__main__":
    unittest.main()
