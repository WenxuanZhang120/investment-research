import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github/workflows/manual-financial-collection.yml"


class ManualCollectionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.content = WORKFLOW.read_text(encoding="utf-8")

    def test_is_manual_only_and_uses_read_only_repository_permission(self):
        self.assertIn("workflow_dispatch:", self.content)
        self.assertNotIn("schedule:", self.content)
        self.assertIn("contents: read", self.content)
        self.assertNotIn("contents: write", self.content)

    def test_requires_versioned_job_choice_and_exact_confirmation(self):
        for job_id in (
            "2026q1_base_resume",
            "2024fy_advanced_inputs",
            "2025fy_advanced_supplement",
            "2025q1_advanced_inputs",
            "2026q1_advanced_supplement",
        ):
            self.assertIn(f"- {job_id}", self.content)
        self.assertIn("COLLECT followed by a colon", self.content)
        self.assertIn("--confirmation", self.content)
        self.assertIn("name: iwencai-collection", self.content)
        self.assertIn("deployment: false", self.content)

    def test_secret_is_only_exposed_to_guarded_collection_step(self):
        self.assertEqual(self.content.count("secrets.IWENCAI_API_KEY"), 1)
        self.assertIn("--action preflight", self.content)
        self.assertIn("--action collect", self.content)
        self.assertIn("scripts/run_guarded_financial_collection.py", self.content)
        self.assertNotIn("run_financial_collection_plan.py collect", self.content)

    def test_uploads_audit_and_raw_evidence_without_repository_writes(self):
        self.assertIn("uses: actions/upload-artifact@v4", self.content)
        self.assertIn("path: action-artifacts/financial-collection/", self.content)
        self.assertIn("if: always()", self.content)
        self.assertNotIn("git push", self.content)
        self.assertNotIn("git commit", self.content)


if __name__ == "__main__":
    unittest.main()
