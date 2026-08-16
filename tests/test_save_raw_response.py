import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.save_raw_response import main, save_raw_response  # noqa: E402
from scripts.public_payload_safety import PublicPayloadSafetyError  # noqa: E402


class SaveRawResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.raw_root = Path(self.temporary_directory.name) / "raw"
        self.fetched_at = datetime(
            2026,
            8,
            8,
            15,
            30,
            45,
            123456,
            tzinfo=timezone(timedelta(hours=8)),
        )
        self.payload = {"status": "ok", "items": [{"value": 1}]}

    def test_saves_snapshot_and_query_log(self) -> None:
        destination = save_raw_response(
            self.payload,
            source="iwencai",
            query="test query",
            raw_root=self.raw_root,
            fetched_at=self.fetched_at,
        )

        self.assertTrue(destination.is_file())
        self.assertEqual(
            destination.relative_to(self.raw_root).parts[:4],
            ("iwencai", "2026", "08", "08"),
        )

        envelope = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(envelope["payload"], self.payload)
        self.assertEqual(envelope["metadata"]["source"], "iwencai")
        self.assertEqual(envelope["metadata"]["query"], "test query")
        self.assertEqual(envelope["metadata"]["schema_version"], 1)
        self.assertEqual(len(envelope["metadata"]["record_id"]), 20)
        self.assertEqual(len(envelope["metadata"]["payload_sha256"]), 64)

        log_path = self.raw_root / "_query_log" / "2026" / "08" / "08.jsonl"
        log_entries = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(log_entries), 1)
        self.assertEqual(log_entries[0]["record_id"], envelope["metadata"]["record_id"])
        self.assertEqual(
            log_entries[0]["raw_relative_path"],
            destination.relative_to(self.raw_root).as_posix(),
        )

    def test_refuses_to_overwrite_existing_snapshot(self) -> None:
        arguments = {
            "source": "iwencai",
            "query": "test query",
            "raw_root": self.raw_root,
            "fetched_at": self.fetched_at,
        }
        first_path = save_raw_response(self.payload, **arguments)

        with self.assertRaises(FileExistsError):
            save_raw_response(self.payload, **arguments)

        self.assertEqual(
            json.loads(first_path.read_text(encoding="utf-8"))["payload"],
            self.payload,
        )

    def test_rejects_unsafe_source_name(self) -> None:
        with self.assertRaises(ValueError):
            save_raw_response(
                self.payload,
                source="../outside",
                query="test query",
                raw_root=self.raw_root,
                fetched_at=self.fetched_at,
            )

    def test_preserves_collection_scope_in_raw_and_query_log(self) -> None:
        scope = {
            "scope_schema_version": 1,
            "scope_type": "market_wide",
            "topic_id": "china_macro_policy_news",
        }
        destination = save_raw_response(
            self.payload,
            source="iwencai",
            query="中国宏观政策",
            raw_root=self.raw_root,
            fetched_at=self.fetched_at,
            collection_scope=scope,
        )
        envelope = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(envelope["metadata"]["collection_scope"], scope)
        log_path = self.raw_root / "_query_log/2026/08/08.jsonl"
        entry = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(entry["collection_scope"], scope)

    def test_preserves_financial_job_and_request_metadata(self) -> None:
        job = {
            "collection_job_schema_version": 1,
            "job_id": "2025fy_test",
            "request_version": 2,
            "expected_period_end": "2025-12-31",
            "query_sha256": "a" * 64,
        }
        request = {"request_schema_version": 1, "page": 3, "limit": 100}
        destination = save_raw_response(
            self.payload,
            source="iwencai",
            query="2025年年报",
            raw_root=self.raw_root,
            fetched_at=self.fetched_at,
            collection_job=job,
            collection_request=request,
        )

        envelope = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(envelope["payload"], self.payload)
        self.assertEqual(envelope["metadata"]["collection_job"], job)
        self.assertEqual(envelope["metadata"]["collection_request"], request)
        log_path = self.raw_root / "_query_log/2026/08/08.jsonl"
        entry = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(entry["collection_job"], job)
        self.assertEqual(entry["collection_request"], request)

    def test_rejects_sensitive_payload_before_creating_public_raw_files(self) -> None:
        with self.assertRaisesRegex(
            PublicPayloadSafetyError, "forbidden credential field"
        ):
            save_raw_response(
                {"data": [], "nested": {"Authorization": "test-only-marker"}},
                source="iwencai",
                query="test query",
                raw_root=self.raw_root,
                fetched_at=self.fetched_at,
            )

        self.assertFalse(self.raw_root.exists())

    def test_command_line_entry_point_saves_local_json(self) -> None:
        input_path = Path(self.temporary_directory.name) / "response.json"
        input_path.write_text(json.dumps(self.payload), encoding="utf-8")
        standard_output = io.StringIO()

        with redirect_stdout(standard_output):
            exit_code = main(
                [
                    "--source",
                    "iwencai",
                    "--query",
                    "command line query",
                    "--input",
                    str(input_path),
                    "--raw-root",
                    str(self.raw_root),
                    "--fetched-at",
                    "2026-08-08T15:30:45.123456+08:00",
                ]
            )

        self.assertEqual(exit_code, 0)
        destination = Path(standard_output.getvalue().strip())
        self.assertEqual(
            json.loads(destination.read_text(encoding="utf-8"))["payload"],
            self.payload,
        )


if __name__ == "__main__":
    unittest.main()
