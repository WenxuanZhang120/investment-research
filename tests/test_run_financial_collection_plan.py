import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.run_financial_collection_plan import (  # noqa: E402
    PlanError,
    inspect_job,
    inspect_plan,
    load_plan,
)
from scripts.save_raw_response import save_raw_response  # noqa: E402


class RunFinancialCollectionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "raw"
        self.job = {
            "job_id": "test_job",
            "period_end": "2025-12-31",
            "purpose": "test",
            "query": "全部A股2025年年报测试问句",
        }
        self.plan = {
            "plan_version": "1.0.0",
            "page_limit": 2,
            "jobs": [self.job],
        }

    def save_page(self, page, total=3, minute=0):
        start = (page - 1) * 2
        rows = [
            {"股票代码": f"{index:06d}.SZ"}
            for index in range(start, min(start + 2, total))
        ]
        payload = {
            "success": True,
            "query": self.job["query"],
            "code_count": total,
            "returned_count": len(rows),
            "page": str(page),
            "limit": "2",
            "has_more": page * 2 < total,
            "datas": rows,
        }
        return save_raw_response(
            payload,
            source="iwencai",
            query=self.job["query"],
            raw_root=self.root,
            fetched_at=datetime(
                2026, 8, 8, 10, minute, tzinfo=timezone(timedelta(hours=8))
            ),
        )

    def test_partial_tail_has_safe_next_page(self):
        self.save_page(1)
        status = inspect_job(self.job, raw_root=self.root)
        self.assertEqual(status["status"], "partial")
        self.assertEqual(status["next_page"], 2)
        self.assertEqual(status["expected_page_count"], 2)

    def test_complete_job_passes_plan_gate(self):
        self.save_page(1)
        self.save_page(2, minute=1)
        status = inspect_plan(self.plan, raw_root=self.root)
        self.assertTrue(status["all_collections_complete"])
        self.assertEqual(status["jobs"][0]["status"], "complete")

    def test_non_contiguous_pages_cannot_resume(self):
        self.save_page(2)
        status = inspect_job(self.job, raw_root=self.root)
        self.assertIn("non_contiguous_pages", status["errors"])
        self.assertIsNone(status["next_page"])

    def test_duplicate_page_is_reported(self):
        self.save_page(1)
        self.save_page(1, minute=1)
        status = inspect_job(self.job, raw_root=self.root)
        self.assertIn("duplicate_pages", status["errors"])

    def test_real_plan_has_unique_known_jobs(self):
        plan = load_plan(REPOSITORY_ROOT / "config/financial_collection_plan.json")
        self.assertEqual(len(plan["jobs"]), 5)
        ids = {job["job_id"] for job in plan["jobs"]}
        self.assertIn("2026q1_base_resume", ids)


if __name__ == "__main__":
    unittest.main()
