import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.normalize_iwencai_financials import (  # noqa: E402
    FinancialNormalizationError,
    build_financial_batch,
    build_financial_tables,
    normalize_financial_snapshots,
)


ANNUAL_SNAPSHOT = REPOSITORY_ROOT / (
    "data/raw/iwencai/2026/08/08/"
    "20260808T192138676741+0800_e53d5eb6b4c8d9fd8b52.json"
)
Q1_SNAPSHOT = REPOSITORY_ROOT / (
    "data/raw/iwencai/2026/08/08/"
    "20260808T192134198330+0800_12929e932cf14a7cda8b.json"
)


def canonical_payload(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class NormalizeIwencaiFinancialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_real_annual_snapshot_builds_long_form_financial_facts(self) -> None:
        built = build_financial_tables(ANNUAL_SNAPSHOT)

        self.assertEqual(len(built["tables"]["financial_reports"]), 3)
        self.assertEqual(len(built["tables"]["financial_facts"]), 45)
        self.assertEqual(built["period_ends"], ["2025-12-31"])
        report = next(
            item
            for item in built["tables"]["financial_reports"]
            if item["security_code"] == "600519.SH"
        )
        self.assertEqual(report["report_type"], "2025FY")
        self.assertEqual(report["filing_date"], "2026-04-17")
        self.assertEqual(report["available_from"], "2026-04-17")
        revenue = next(
            item
            for item in built["tables"]["financial_facts"]
            if item["security_code"] == "600519.SH"
            and item["canonical_field_name"] == "revenue"
        )
        self.assertEqual(revenue["statement_type"], "income_statement")
        self.assertEqual(revenue["value_nature"], "duration_ytd")
        self.assertEqual(revenue["unit"], "CNY")
        self.assertEqual(
            revenue["field_lineage"]["revenue"]["raw_field_name"],
            "营业收入[20251231]",
        )

    def test_missing_bank_items_remain_explicit_null_facts(self) -> None:
        built = build_financial_tables(ANNUAL_SNAPSHOT)

        bank_missing = {
            item["canonical_field_name"]
            for item in built["tables"]["financial_facts"]
            if item["security_code"] == "600036.SH"
            and item["value_status"] == "missing_in_source"
        }
        self.assertEqual(
            bank_missing,
            {"accounts_receivable", "inventory", "monetary_funds"},
        )
        self.assertTrue(
            all(
                item["value"] is None
                for item in built["tables"]["financial_facts"]
                if item["security_code"] == "600036.SH"
                and item["value_status"] == "missing_in_source"
            )
        )

    def test_two_period_batch_preserves_point_in_time_versions(self) -> None:
        built = build_financial_batch([ANNUAL_SNAPSHOT, Q1_SNAPSHOT])

        coverage = built["coverage"]
        self.assertEqual(coverage["source_snapshot_count"], 2)
        self.assertEqual(coverage["query_count"], 2)
        self.assertEqual(coverage["security_count"], 3)
        self.assertEqual(coverage["period_ends"], ["2025-12-31", "2026-03-31"])
        self.assertEqual(coverage["report_types"], ["2025FY", "2026Q1"])
        self.assertEqual(coverage["financial_report_count"], 6)
        self.assertEqual(coverage["financial_fact_count"], 90)
        self.assertEqual(coverage["present_fact_count"], 84)
        self.assertEqual(coverage["missing_fact_count"], 6)
        self.assertEqual(
            coverage["fact_count_by_statement"],
            {
                "income_statement": 30,
                "balance_sheet": 36,
                "cash_flow_statement": 24,
            },
        )
        self.assertTrue(
            all(item["page_count"] == 1 for item in coverage["query_coverage"])
        )
        fact_keys = [
            (
                item["security_code"],
                item["period_end"],
                item["canonical_field_name"],
                item["raw_record_id"],
            )
            for item in built["tables"]["financial_facts"]
        ]
        self.assertEqual(len(fact_keys), len(set(fact_keys)))

    def test_combines_complete_paginated_financial_query(self) -> None:
        original = json.loads(ANNUAL_SNAPSHOT.read_text(encoding="utf-8"))
        rows = original["payload"]["datas"]
        snapshots = []
        for page, page_rows in ((1, rows[:2]), (2, rows[2:])):
            document = json.loads(json.dumps(original, ensure_ascii=False))
            document["payload"]["datas"] = page_rows
            document["payload"]["page"] = str(page)
            document["payload"]["limit"] = "2"
            document["payload"]["code_count"] = 3
            document["payload"]["returned_count"] = len(page_rows)
            document["payload"]["has_more"] = page == 1
            document["metadata"]["record_id"] = f"page-{page}"
            document["metadata"]["fetched_at"] = (
                f"2026-08-08T20:00:0{page}+08:00"
            )
            document["metadata"]["payload_sha256"] = hashlib.sha256(
                canonical_payload(document["payload"])
            ).hexdigest()
            snapshot = self.root / f"page-{page}.json"
            snapshot.write_text(
                json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )
            snapshots.append(snapshot)

        built = build_financial_batch(
            snapshots, repository_root=self.root
        )

        self.assertEqual(len(built["tables"]["financial_reports"]), 3)
        self.assertEqual(len(built["tables"]["financial_facts"]), 45)
        self.assertEqual(built["coverage"]["query_count"], 1)
        self.assertEqual(
            built["coverage"]["query_coverage"][0]["page_count"],
            2,
        )

        with self.assertRaisesRegex(FinancialNormalizationError, "incomplete"):
            build_financial_batch(
                [snapshots[0]], repository_root=self.root
            )

    def test_prefers_audited_request_pagination_when_payload_omits_it(self) -> None:
        document = json.loads(ANNUAL_SNAPSHOT.read_text(encoding="utf-8"))
        document["payload"].pop("page", None)
        document["payload"].pop("limit", None)
        document["metadata"]["collection_job"] = {
            "collection_job_schema_version": 1,
            "job_id": "2025fy_test",
            "request_version": 2,
            "expected_period_end": "2025-12-31",
            "query_sha256": "a" * 64,
        }
        document["metadata"]["collection_request"] = {
            "request_schema_version": 1,
            "page": 1,
            "limit": 100,
        }
        document["metadata"]["payload_sha256"] = hashlib.sha256(
            canonical_payload(document["payload"])
        ).hexdigest()
        snapshot = self.root / "metadata-pagination.json"
        snapshot.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        built = build_financial_tables(snapshot, repository_root=self.root)

        self.assertEqual(built["page"], 1)
        self.assertEqual(built["limit"], 100)

    def test_rejects_conflicting_request_and_payload_pagination(self) -> None:
        document = json.loads(ANNUAL_SNAPSHOT.read_text(encoding="utf-8"))
        document["metadata"]["collection_request"] = {
            "request_schema_version": 1,
            "page": 1,
            "limit": 99,
        }
        snapshot = self.root / "conflicting-pagination.json"
        snapshot.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(
            FinancialNormalizationError, "conflicts with raw response limit"
        ):
            build_financial_tables(snapshot, repository_root=self.root)

    def test_rejects_collection_job_period_mismatch_without_remapping(self) -> None:
        document = json.loads(ANNUAL_SNAPSHOT.read_text(encoding="utf-8"))
        document["metadata"]["collection_job"] = {
            "collection_job_schema_version": 1,
            "job_id": "2024fy_test",
            "request_version": 1,
            "expected_period_end": "2024-12-31",
            "query_sha256": "a" * 64,
        }
        snapshot = self.root / "wrong-job-period.json"
        snapshot.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(
            FinancialNormalizationError, "does not match collection job"
        ):
            build_financial_tables(snapshot, repository_root=self.root)

    def test_rejects_explicit_provider_failure(self) -> None:
        document = json.loads(ANNUAL_SNAPSHOT.read_text(encoding="utf-8"))
        document["payload"]["status_code"] = 1
        document["metadata"]["payload_sha256"] = hashlib.sha256(
            canonical_payload(document["payload"])
        ).hexdigest()
        snapshot = self.root / "provider-failure.json"
        snapshot.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(
            FinancialNormalizationError, "failed status_code"
        ):
            build_financial_tables(snapshot, repository_root=self.root)

    def test_writes_atomic_bundle_and_refuses_overwrite(self) -> None:
        destination = normalize_financial_snapshots(
            [ANNUAL_SNAPSHOT, Q1_SNAPSHOT],
            normalized_root=self.root,
        )

        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["normalizer_version"], "1.3.0")
        self.assertEqual(manifest["bundle_schema_version"], 2)
        self.assertEqual(manifest["mapping_version"], "3.4.0")
        self.assertEqual(
            manifest["tables"]["financial_reports"]["record_count"],
            6,
        )
        self.assertEqual(
            manifest["tables"]["financial_facts"]["record_count"],
            90,
        )
        self.assertEqual(
            len(manifest["tables"]["financial_facts"]["partitions"]),
            6,
        )
        self.assertFalse((destination / "financial_facts.jsonl").exists())

        with self.assertRaises(FileExistsError):
            normalize_financial_snapshots(
                [ANNUAL_SNAPSHOT, Q1_SNAPSHOT],
                normalized_root=self.root,
            )

    def test_rejects_tampered_raw_payload(self) -> None:
        document = json.loads(ANNUAL_SNAPSHOT.read_text(encoding="utf-8"))
        document["payload"]["datas"][0]["股票简称"] = "被修改"
        snapshot = self.root / "tampered.json"
        snapshot.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(FinancialNormalizationError, "checksum"):
            build_financial_tables(snapshot, repository_root=self.root)

    def test_rejects_filing_date_after_fetch_time(self) -> None:
        document = json.loads(ANNUAL_SNAPSHOT.read_text(encoding="utf-8"))
        document["payload"]["datas"][0]["公告日期[20251231]"] = "20270101"
        document["metadata"]["payload_sha256"] = hashlib.sha256(
            canonical_payload(document["payload"])
        ).hexdigest()
        snapshot = self.root / "future-filing.json"
        snapshot.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(
            FinancialNormalizationError,
            "later than fetched_at",
        ):
            build_financial_tables(snapshot, repository_root=self.root)

    def test_row_without_any_period_data_is_recorded_without_fabrication(self) -> None:
        document = json.loads(ANNUAL_SNAPSHOT.read_text(encoding="utf-8"))
        row = document["payload"]["datas"][0]
        for key in list(row):
            if "[20251231]" in key:
                del row[key]
        document["metadata"]["payload_sha256"] = hashlib.sha256(
            canonical_payload(document["payload"])
        ).hexdigest()
        snapshot = self.root / "missing-report.json"
        snapshot.write_text(
            json.dumps(document, ensure_ascii=False),
            encoding="utf-8",
        )

        built = build_financial_batch([snapshot], repository_root=self.root)

        self.assertEqual(built["coverage"]["source_security_count"], 3)
        self.assertEqual(built["coverage"]["security_count"], 2)
        self.assertEqual(built["coverage"]["missing_report_row_count"], 1)
        missing = built["coverage"]["missing_report_rows"][0]
        self.assertEqual(missing["security_code"], row["股票代码"])
        self.assertEqual(missing["period_end"], "2025-12-31")
        self.assertEqual(missing["reason"], "report_not_present_in_source")
        self.assertFalse(
            any(
                report["security_code"] == row["股票代码"]
                for report in built["tables"]["financial_reports"]
            )
        )

    def test_rejects_duplicate_code_across_report_and_missing_report_rows(self) -> None:
        document = json.loads(ANNUAL_SNAPSHOT.read_text(encoding="utf-8"))
        missing_row = document["payload"]["datas"][0]
        report_row = document["payload"]["datas"][1]
        for key in list(missing_row):
            if "[20251231]" in key:
                del missing_row[key]
        missing_row["股票代码"] = report_row["股票代码"]
        document["metadata"]["payload_sha256"] = hashlib.sha256(
            canonical_payload(document["payload"])
        ).hexdigest()
        snapshot = self.root / "duplicate-report-status.json"
        snapshot.write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            FinancialNormalizationError,
            "duplicate source security codes",
        ):
            build_financial_batch([snapshot], repository_root=self.root)


if __name__ == "__main__":
    unittest.main()
