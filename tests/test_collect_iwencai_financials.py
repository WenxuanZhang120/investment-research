import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.collect_iwencai_financials import (  # noqa: E402
    CollectionError,
    _request_with_retry,
    collect_query,
)


class CollectIwencaiFinancialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.raw_root = Path(self.temporary_directory.name) / "raw"
        self.api_key_patch = patch.dict(
            os.environ,
            {"IWENCAI_API_KEY": "test-secret-never-saved"},
        )
        self.api_key_patch.start()
        self.addCleanup(self.api_key_patch.stop)

    @staticmethod
    def response(page, total=5):
        start = (page - 1) * 2
        rows = []
        for index in range(start, min(start + 2, total)):
            rows.append(
                {
                    "股票代码": f"{index:06d}.SZ",
                    "股票简称": f"证券{index}",
                    "公告日期[20251231]": "20260401",
                    "报告期[20251231]": "2025年年报",
                    "营业收入[20251231]": index + 1,
                }
            )
        return {"datas": rows, "code_count": total, "chunks_info": {}}

    def test_collects_and_saves_every_page_without_credentials(self) -> None:
        calls = []

        def fake_request(**kwargs):
            calls.append(kwargs)
            return self.response(kwargs["page"])

        result = collect_query(
            "全部A股2025年年报",
            limit=2,
            raw_root=self.raw_root,
            request_page=fake_request,
        )

        self.assertEqual(result["page_count"], 3)
        self.assertEqual(result["record_count"], 5)
        self.assertEqual([call["page"] for call in calls], [1, 2, 3])
        self.assertTrue(all(call["api_key"] == "test-secret-never-saved" for call in calls))
        for path_text in result["snapshot_paths"]:
            document = json.loads(Path(path_text).read_text(encoding="utf-8"))
            self.assertNotIn("test-secret-never-saved", json.dumps(document))
            self.assertNotIn("Authorization", json.dumps(document))
        query_log = self.raw_root / "_query_log" / Path(
            result["snapshot_paths"][0]
        ).relative_to(self.raw_root).parts[1] / Path(
            result["snapshot_paths"][0]
        ).relative_to(self.raw_root).parts[2] / (
            Path(result["snapshot_paths"][0]).relative_to(self.raw_root).parts[3]
            + ".jsonl"
        )
        self.assertEqual(len(query_log.read_text(encoding="utf-8").splitlines()), 3)

    def test_preserves_provider_response_and_audits_request_metadata(self) -> None:
        provider_response = self.response(1, total=1)
        provider_response["status_code"] = 0
        job = {
            "collection_job_schema_version": 1,
            "job_id": "2025fy_test",
            "request_version": 2,
            "expected_period_end": "2025-12-31",
            "query_sha256": "a" * 64,
        }

        result = collect_query(
            "全部沪深主板A股2025年年报",
            limit=100,
            raw_root=self.raw_root,
            request_page=lambda **kwargs: provider_response,
            collection_job=job,
        )

        document = json.loads(
            Path(result["snapshot_paths"][0]).read_text(encoding="utf-8")
        )
        self.assertEqual(document["payload"], provider_response)
        self.assertNotIn("page", document["payload"])
        self.assertEqual(document["metadata"]["collection_job"], job)
        self.assertEqual(
            document["metadata"]["collection_request"],
            {"request_schema_version": 1, "page": 1, "limit": 100},
        )

    def test_saves_explicit_provider_failure_before_stopping(self) -> None:
        response = self.response(1, total=1)
        response["status_code"] = 1
        with self.assertRaisesRegex(CollectionError, "status_code=1"):
            collect_query(
                "全部A股2025年年报",
                raw_root=self.raw_root,
                request_page=lambda **kwargs: response,
            )
        snapshots = list(self.raw_root.glob("iwencai/*/*/*/*.json"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(
            json.loads(snapshots[0].read_text(encoding="utf-8"))["payload"],
            response,
        )

    def test_rejects_credential_headers_in_provider_body_without_rewriting(self) -> None:
        response = self.response(1, total=1)
        response["claw_headers"] = {"Authorization": "dummy"}
        with self.assertRaisesRegex(CollectionError, "claw_headers"):
            collect_query(
                "全部A股2025年年报",
                raw_root=self.raw_root,
                request_page=lambda **kwargs: response,
            )
        self.assertEqual(
            list(self.raw_root.glob("iwencai/*/*/*/*.json")), []
        )

    def test_rejects_nested_session_identifier_before_public_raw_write(self) -> None:
        response = self.response(1, total=1)
        response["metadata"] = {"sessionId": "test-only-marker"}
        with self.assertRaisesRegex(CollectionError, "forbidden credential field"):
            collect_query(
                "全部A股2025年年报",
                raw_root=self.raw_root,
                request_page=lambda **kwargs: response,
            )
        self.assertEqual(
            list(self.raw_root.glob("iwencai/*/*/*/*.json")), []
        )

    def test_bad_period_stops_after_first_raw_snapshot(self) -> None:
        calls = []
        response = self.response(1, total=5)
        response["datas"][0] = {
            "股票代码": "000000.SZ",
            "股票简称": "证券0",
            "公告日期[20260331]": "20260401",
            "报告期[20261231]": "2026年年报",
            "营业收入[20260331]": 1,
        }
        query = "全部沪深主板A股2025年年报"
        job = {
            "collection_job_schema_version": 1,
            "job_id": "2025fy_test",
            "request_version": 2,
            "expected_period_end": "2025-12-31",
            "query_sha256": "a" * 64,
        }

        def fake_request(**kwargs):
            calls.append(kwargs["page"])
            return response

        with self.assertRaisesRegex(CollectionError, "period contract mismatch"):
            collect_query(
                query,
                limit=2,
                raw_root=self.raw_root,
                request_page=fake_request,
                collection_job=job,
            )

        self.assertEqual(calls, [1])
        snapshots = list(self.raw_root.glob("iwencai/*/*/*/*.json"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(
            json.loads(snapshots[0].read_text(encoding="utf-8"))["payload"],
            response,
        )

    def test_saves_response_before_reporting_changed_total(self) -> None:
        def fake_request(**kwargs):
            total = 5 if kwargs["page"] == 1 else 6
            return self.response(kwargs["page"], total=total)

        with self.assertRaisesRegex(CollectionError, "code_count changed"):
            collect_query(
                "全部A股2025年年报",
                limit=2,
                raw_root=self.raw_root,
                request_page=fake_request,
            )

        self.assertEqual(len(list(self.raw_root.glob("iwencai/*/*/*/*.json"))), 2)

    def test_collects_a_validated_continuation_segment(self) -> None:
        calls = []

        def fake_request(**kwargs):
            calls.append(kwargs)
            return self.response(kwargs["page"])

        result = collect_query(
            "全部A股2026年一季报",
            start_page=2,
            limit=2,
            raw_root=self.raw_root,
            request_page=fake_request,
        )

        self.assertEqual([call["page"] for call in calls], [2, 3])
        self.assertEqual(result["start_page"], 2)
        self.assertEqual(result["end_page"], 3)
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["record_count"], 3)
        self.assertEqual(result["query_record_count"], 5)
        self.assertFalse(result["complete_query"])

    def test_page_budget_saves_a_validated_partial_segment(self) -> None:
        calls = []

        def fake_request(**kwargs):
            calls.append(kwargs)
            return self.response(kwargs["page"])

        result = collect_query(
            "全部A股2025年年报",
            limit=2,
            page_budget=2,
            raw_root=self.raw_root,
            request_page=fake_request,
        )

        self.assertEqual([call["page"] for call in calls], [1, 2])
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["record_count"], 4)
        self.assertFalse(result["complete_query"])
        self.assertFalse(result["reached_query_end"])
        self.assertEqual(result["remaining_page_count"], 1)
        self.assertEqual(result["next_page"], 3)
        self.assertEqual(
            len(list(self.raw_root.glob("iwencai/*/*/*/*.json"))), 2
        )

    def test_requires_environment_api_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(CollectionError, "IWENCAI_API_KEY"):
                collect_query(
                    "query",
                    raw_root=self.raw_root,
                    request_page=lambda **kwargs: self.response(1),
                )

    def test_does_not_retry_non_retryable_auth_or_quota_error(self) -> None:
        calls = []

        def fail(**kwargs):
            calls.append(kwargs)
            raise CollectionError("HTTP 401 quota", retryable=False)

        with self.assertRaisesRegex(CollectionError, "HTTP 401 quota"):
            _request_with_retry(fail, query="q")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
