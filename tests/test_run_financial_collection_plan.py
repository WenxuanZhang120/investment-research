import io
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.run_financial_collection_plan import (  # noqa: E402
    PlanError,
    collect_job,
    inspect_job,
    inspect_plan,
    load_plan,
    main,
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

    def save_contract_page(
        self,
        *,
        query,
        request_version=2,
        response_period="20251231",
        expected_period="2025-12-31",
        with_job=True,
        page=1,
        limit=100,
        total=None,
        security_codes=None,
        minute=0,
    ):
        security_codes = security_codes or ["000001.SZ"]
        total = len(security_codes) if total is None else total
        payload = {
            "status_code": 0,
            "code_count": total,
            "datas": [
                {
                    "股票代码": security_code,
                    "股票简称": "平安银行",
                    f"公告日期[{response_period}]": "20260401",
                    f"报告期[{response_period}]": "2025年年报",
                    f"营业收入[{response_period}]": 1,
                }
                for security_code in security_codes
            ],
        }
        job = (
            {
                "collection_job_schema_version": 1,
                "job_id": self.job["job_id"],
                "request_version": request_version,
                "expected_period_end": expected_period,
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            }
            if with_job
            else None
        )
        return save_raw_response(
            payload,
            source="iwencai",
            query=query,
            raw_root=self.root,
            fetched_at=datetime(
                2026, 8, 8, 11, minute, tzinfo=timezone(timedelta(hours=8))
            ),
            collection_job=job,
            collection_request={
                "request_schema_version": 1,
                "page": page,
                "limit": limit,
            },
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

    def test_stable_job_identity_detects_and_quarantines_query_drift(self):
        self.job["request_version"] = 2
        configured_query = self.job["query"]
        actual_query = configured_query + "（明确口径）"
        self.save_contract_page(query=actual_query)

        status = inspect_job(self.job, raw_root=self.root)

        self.assertEqual(status["status"], "quarantined")
        self.assertEqual(status["snapshot_count"], 1)
        self.assertIn("collection_job_query_drift", status["errors"])

    def test_current_bad_period_is_quarantined_and_not_recollected(self):
        self.job["request_version"] = 2
        self.save_contract_page(
            query=self.job["query"],
            response_period="20260331",
        )

        status = inspect_job(self.job, raw_root=self.root)
        self.assertEqual(status["status"], "quarantined")
        self.assertIn("unexpected_financial_period", status["errors"])
        self.assertIsNone(status["next_page"])
        self.assertEqual(len(status["quarantine_fingerprints"]), 1)
        with patch("scripts.run_financial_collection_plan.collect_query") as mocked:
            with self.assertRaisesRegex(PlanError, "cannot resume safely"):
                collect_job(self.plan, "test_job", raw_root=self.root)
        mocked.assert_not_called()

    def test_small_v2_subset_is_quarantined_and_not_recollected(self):
        self.job.update(
            {
                "request_version": 2,
                "minimum_expected_count": 3000,
            }
        )
        self.save_contract_page(query=self.job["query"])

        status = inspect_job(self.job, raw_root=self.root)

        self.assertEqual(status["status"], "quarantined")
        self.assertIn("below_minimum_expected_count", status["errors"])
        self.assertEqual(status["minimum_expected_count"], 3000)
        self.assertEqual(status["reported_total_count"], 1)
        self.assertIsNone(status["next_page"])
        with patch("scripts.run_financial_collection_plan.collect_query") as mocked:
            with self.assertRaisesRegex(PlanError, "cannot resume safely"):
                collect_job(self.plan, "test_job", raw_root=self.root)
        mocked.assert_not_called()

    def test_continuation_duplicate_is_quarantined_by_plan_postflight(self):
        self.job["request_version"] = 2
        self.save_contract_page(
            query=self.job["query"],
            page=1,
            limit=2,
            total=3,
            security_codes=["000001.SZ", "000002.SZ"],
        )

        def save_duplicate_continuation(*args, **kwargs):
            self.save_contract_page(
                query=self.job["query"],
                page=2,
                limit=2,
                total=3,
                security_codes=["000001.SZ"],
                minute=1,
            )
            return {"saved_pages": [2]}

        with patch(
            "scripts.run_financial_collection_plan.collect_query",
            side_effect=save_duplicate_continuation,
        ):
            with self.assertRaisesRegex(
                PlanError, "post-collection validation failed"
            ):
                collect_job(self.plan, "test_job", raw_root=self.root)

        status = inspect_job(self.job, raw_root=self.root)
        self.assertEqual(status["status"], "quarantined")
        self.assertIn("duplicate_security_codes", status["errors"])
        self.assertIn("unique_security_count_mismatch", status["errors"])
        self.assertEqual(status["returned_row_count"], 3)
        self.assertEqual(status["unique_security_count"], 2)
        with patch("scripts.run_financial_collection_plan.collect_query") as mocked:
            with self.assertRaisesRegex(PlanError, "cannot resume safely"):
                collect_job(self.plan, "test_job", raw_root=self.root)
        mocked.assert_not_called()

    def test_historical_bad_query_is_visible_but_does_not_pollute_new_revision(self):
        old_query = self.job["query"]
        self.job.update(
            {
                "request_version": 2,
                "query": old_query + "（修正版）",
                "historical_queries": [
                    {"request_version": 1, "query": old_query}
                ],
            }
        )
        self.save_contract_page(
            query=old_query,
            response_period="20260331",
            with_job=False,
        )

        status = inspect_job(self.job, raw_root=self.root)

        self.assertEqual(status["status"], "not_started")
        self.assertEqual(status["snapshot_count"], 0)
        self.assertEqual(status["historical_snapshot_count"], 1)
        self.assertEqual(len(status["historical_quarantine_fingerprints"]), 1)

    def test_new_revision_rejects_query_only_identity(self):
        self.job["request_version"] = 2
        self.save_contract_page(query=self.job["query"], with_job=False)

        status = inspect_job(self.job, raw_root=self.root)

        self.assertEqual(status["status"], "quarantined")
        self.assertEqual(status["snapshot_count"], 0)
        self.assertEqual(status["unbound_current_snapshot_count"], 1)
        self.assertIn("collection_job_identity_missing", status["errors"])

    def test_real_plan_has_unique_known_jobs(self):
        plan = load_plan(REPOSITORY_ROOT / "config/financial_collection_plan.json")
        self.assertEqual(len(plan["jobs"]), 5)
        ids = {job["job_id"] for job in plan["jobs"]}
        self.assertIn("2026q1_base_resume", ids)
        current = next(
            job for job in plan["jobs"]
            if job["job_id"] == "2025fy_advanced_supplement"
        )
        self.assertEqual(current["request_version"], 3)
        self.assertTrue(current["query"].startswith("全部沪深主板A股"))
        self.assertIn("查询2025年12月31日报告期的", current["query"])
        self.assertEqual(len(current["historical_queries"]), 3)
        self.assertEqual(current["historical_queries"][0]["request_version"], 2)
        universe = json.loads(
            (REPOSITORY_ROOT / "config/investment_universe.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            current["minimum_expected_count"],
            universe["stocks"]["minimum_expected_count"],
        )

    def test_v2_main_board_threshold_cannot_drift_from_universe(self):
        plan_path = self.root.parent / "financial_collection_plan.json"
        universe_path = self.root.parent / "investment_universe.json"
        plan = json.loads(json.dumps(self.plan))
        plan["jobs"][0].update(
            {
                "request_version": 2,
                "universe_id": "cn_sh_sz_main_board_a",
                "minimum_expected_count": 2999,
            }
        )
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        universe_path.write_text(
            (REPOSITORY_ROOT / "config/investment_universe.json").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            PlanError, "configured stock universe threshold"
        ):
            load_plan(plan_path)

    def test_cli_reports_runtime_error_without_traceback(self):
        plan_path = self.root.parent / "plan.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        stderr = io.StringIO()
        with patch(
            "scripts.run_financial_collection_plan.collect_job",
            side_effect=RuntimeError("quota unavailable"),
        ), redirect_stderr(stderr):
            return_code = main(
                [
                    "--plan",
                    str(plan_path),
                    "--raw-root",
                    str(self.root),
                    "collect",
                    "--job",
                    "test_job",
                ]
            )
        self.assertEqual(return_code, 1)
        self.assertEqual(stderr.getvalue(), "error: quota unavailable\n")
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
