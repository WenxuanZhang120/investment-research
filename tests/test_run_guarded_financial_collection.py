import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.run_guarded_financial_collection import (  # noqa: E402
    run_guarded_collection,
)
from scripts.save_raw_response import save_raw_response  # noqa: E402


class GuardedFinancialCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        (self.root / "data/raw").mkdir(parents=True)
        self.raw_root = self.root / "data/raw"
        self.artifact_root = self.root / ".artifacts/financial"
        self.job = {
            "job_id": "test_job",
            "period_end": "2025-12-31",
            "purpose": "test",
            "query": "全部A股2025年年报测试问句",
        }
        self.plan_path = self.root / "config/financial_collection_plan.json"
        self.plan_path.write_text(
            json.dumps(
                {
                    "plan_version": "test-plan",
                    "source": "iwencai",
                    "page_limit": 2,
                    "jobs": [self.job],
                }
            ),
            encoding="utf-8",
        )
        self.policy_path = self.root / "config/collection_safety.json"
        self.policy_path.write_text(
            json.dumps(
                {
                    "policy_version": "test-policy",
                    "source": "iwencai",
                    "credential_environment_variable": "IWENCAI_API_KEY",
                    "confirmation_prefix": "COLLECT:",
                    "max_pages_per_run": 2,
                    "request_timeout_seconds": 30,
                    "allowed_job_ids": ["test_job"],
                }
            ),
            encoding="utf-8",
        )
        self.save_page(1, minute=0)
        self.created_at = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)

    def save_page(self, page, *, minute):
        start = (page - 1) * 2
        rows = [
            {"股票代码": f"{index:06d}.SZ"}
            for index in range(start, min(start + 2, 3))
        ]
        return save_raw_response(
            {
                "success": True,
                "query": self.job["query"],
                "code_count": 3,
                "returned_count": len(rows),
                "page": str(page),
                "limit": "2",
                "has_more": page == 1,
                "datas": rows,
            },
            source="iwencai",
            query=self.job["query"],
            raw_root=self.raw_root,
            fetched_at=datetime(
                2026, 8, 9, 10, minute, tzinfo=timezone(timedelta(hours=8))
            ),
        )

    def execute(self, **kwargs):
        return run_guarded_collection(
            job_id="test_job",
            plan_path=self.plan_path,
            policy_path=self.policy_path,
            raw_root=self.raw_root,
            artifact_root=self.artifact_root,
            repository_root=self.root,
            created_at=self.created_at,
            **kwargs,
        )

    def test_preflight_is_offline_and_needs_no_credential(self):
        calls = []
        with patch.dict(os.environ, {}, clear=True):
            result = self.execute(
                action="preflight",
                collector=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
        self.assertEqual(calls, [])
        audit = result["audit"]
        self.assertEqual(audit["status"], "preflight_completed")
        self.assertTrue(audit["preflight"]["ready_for_collection_request"])
        self.assertFalse(audit["preflight"]["credential_present"])
        self.assertEqual(audit["preflight"]["planned_start_page"], 2)
        self.assertEqual(audit["preflight"]["planned_page_count"], 1)
        content = (result["artifact_path"] / "audit.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(str(self.root), content)

    def test_collect_requires_exact_confirmation_and_credential(self):
        calls = []
        with patch.dict(
            os.environ, {"IWENCAI_API_KEY": "test-secret-never-saved"}
        ):
            wrong = self.execute(
                action="collect",
                confirmation="COLLECT:wrong_job",
                collector=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
        self.assertEqual(wrong["audit"]["status"], "blocked")
        self.assertIn(
            "confirmation_mismatch",
            wrong["audit"]["preflight"]["blocked_reasons"],
        )
        self.assertEqual(calls, [])

        second_time = self.created_at.replace(minute=1)
        with patch.dict(os.environ, {}, clear=True):
            missing = run_guarded_collection(
                job_id="test_job",
                action="collect",
                confirmation="COLLECT:test_job",
                plan_path=self.plan_path,
                policy_path=self.policy_path,
                raw_root=self.raw_root,
                artifact_root=self.artifact_root,
                repository_root=self.root,
                collector=lambda *args, **kwargs: calls.append((args, kwargs)),
                created_at=second_time,
            )
        self.assertEqual(missing["audit"]["status"], "blocked")
        self.assertIn(
            "credential_missing",
            missing["audit"]["preflight"]["blocked_reasons"],
        )
        self.assertEqual(calls, [])

    def test_authorized_collection_bundles_only_portable_raw_evidence(self):
        def fake_collector(plan, job_id, **kwargs):
            self.assertEqual(job_id, "test_job")
            self.assertEqual(kwargs["page_budget"], 1)
            self.assertEqual(kwargs["timeout"], 30)
            path = self.save_page(2, minute=1)
            return {
                "query": self.job["query"],
                "start_page": 2,
                "end_page": 2,
                "page_count": 1,
                "record_count": 1,
                "reached_query_end": True,
                "snapshot_paths": [str(path)],
            }

        with patch.dict(
            os.environ, {"IWENCAI_API_KEY": "test-secret-never-saved"}
        ):
            result = self.execute(
                action="collect",
                confirmation="COLLECT:test_job",
                collector=fake_collector,
            )
        audit = result["audit"]
        self.assertEqual(audit["status"], "succeeded")
        self.assertEqual(audit["new_raw_snapshot_count"], 1)
        self.assertTrue(audit["raw_first_preserved"])
        self.assertNotIn("snapshot_paths", audit["collection_result"])
        self.assertNotIn("query", audit["collection_result"])
        files = [path for path in result["artifact_path"].rglob("*") if path.is_file()]
        self.assertTrue(any(path.name == "audit.json" for path in files))
        self.assertTrue(any(path.suffix == ".jsonl" for path in files))
        self.assertTrue(any("data/raw/iwencai" in path.as_posix() for path in files))
        combined = "".join(path.read_text(encoding="utf-8") for path in files)
        self.assertNotIn("test-secret-never-saved", combined)
        self.assertNotIn(str(self.root), combined)

    def test_raw_evidence_survives_a_collection_failure(self):
        def failing_collector(plan, job_id, **kwargs):
            self.save_page(2, minute=1)
            raise RuntimeError("simulated failure after Raw save")

        with patch.dict(
            os.environ, {"IWENCAI_API_KEY": "test-secret-never-saved"}
        ):
            result = self.execute(
                action="collect",
                confirmation="COLLECT:test_job",
                collector=failing_collector,
            )
        self.assertEqual(result["audit"]["status"], "failed")
        self.assertEqual(result["audit"]["runtime_error_type"], "RuntimeError")
        self.assertEqual(result["audit"]["new_raw_snapshot_count"], 1)
        self.assertTrue(result["audit"]["raw_first_preserved"])
        persisted = json.loads(
            (result["artifact_path"] / "audit.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("simulated failure", json.dumps(persisted))


if __name__ == "__main__":
    unittest.main()
