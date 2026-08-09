import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github/workflows/daily-offline-pipeline.yml"


class DailyOfflineWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.content = WORKFLOW.read_text(encoding="utf-8")

    def test_supports_manual_and_weekday_shanghai_evening_runs(self):
        self.assertIn("workflow_dispatch:", self.content)
        self.assertRegex(self.content, re.compile(r'cron: ["\']0 10 \* \* 1-5["\']'))
        self.assertIn("18:00 Asia/Shanghai", self.content)

    def test_has_read_only_non_overlapping_execution(self):
        self.assertRegex(
            self.content,
            re.compile(r"permissions:\s+contents: read", re.MULTILINE),
        )
        self.assertIn("group: daily-offline-research-pipeline", self.content)
        self.assertIn("cancel-in-progress: false", self.content)
        self.assertNotIn("contents: write", self.content)

    def test_runs_offline_entry_point_and_uploads_only_run_reports(self):
        self.assertIn("python3 scripts/run_daily_pipeline.py", self.content)
        self.assertIn("--reports-root reports/daily/action-runs", self.content)
        self.assertIn("uses: actions/upload-artifact@v4", self.content)
        self.assertIn("path: reports/daily/action-runs/", self.content)
        self.assertIn("retention-days: 30", self.content)
        self.assertIn("if: always()", self.content)

    def test_does_not_configure_credentials_collection_or_repository_writes(self):
        forbidden = (
            "IWENCAI_API_KEY",
            "run_financial_collection_plan.py collect",
            "git push",
            "git commit",
            "pull_request_target",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, self.content)


if __name__ == "__main__":
    unittest.main()
