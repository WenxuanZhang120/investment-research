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

    def test_monitoring_completion_uses_deduplicated_records_and_audited_reports(self):
        result = audit_system(REPOSITORY_ROOT)
        by_name = {item["requirement"]: item for item in result["results"]}
        monitoring = by_name["announcement_and_news_monitoring"]
        self.assertTrue(monitoring["achieved"])
        minimums = json.loads(
            (REPOSITORY_ROOT / "config/system_completion_requirements.json").read_text()
        )["minimum_counts"]
        self.assertGreaterEqual(
            monitoring["evidence"]["real_event_count"], minimums["real_events"]
        )
        self.assertGreater(monitoring["evidence"]["real_news_count"], 0)
        self.assertTrue(monitoring["evidence"]["event_report_manifests"])
        self.assertTrue(monitoring["evidence"]["news_report_manifests"])


if __name__ == "__main__":
    unittest.main()
