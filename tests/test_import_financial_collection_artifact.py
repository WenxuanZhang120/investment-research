import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.import_financial_collection_artifact import (  # noqa: E402
    ArtifactImportError,
    import_artifact,
)
from scripts.save_raw_response import save_raw_response  # noqa: E402


class ImportFinancialCollectionArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source_root = self.base / "source"
        self.destination_root = self.base / "destination"
        self.raw_root = self.source_root / "data/raw"
        self.destination_root.mkdir()
        self.snapshot = save_raw_response(
            {
                "success": True,
                "query": "测试财务采集",
                "code_count": 1,
                "returned_count": 1,
                "page": "1",
                "limit": "100",
                "has_more": False,
                "datas": [{"股票代码": "000001.SZ"}],
            },
            source="iwencai",
            query="测试财务采集",
            raw_root=self.raw_root,
            fetched_at=datetime(
                2026, 8, 9, 10, 0, tzinfo=timezone(timedelta(hours=8))
            ),
        )
        self.snapshot_public = self.snapshot.relative_to(self.source_root).as_posix()
        self.log = self.raw_root / "_query_log/2026/08/09.jsonl"
        self.log_public = self.log.relative_to(self.source_root).as_posix()
        self.bundle = self.base / "downloaded-artifact/2026/08/09/test-bundle"
        for source, public_path in (
            (self.snapshot, self.snapshot_public),
            (self.log, self.log_public),
        ):
            target = self.bundle / "repository" / public_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self.audit = {
            "collection_audit_schema_version": 1,
            "created_at": "2026-08-09T02:00:00.000000+00:00",
            "status": "succeeded",
            "preflight": {
                "policy_version": "test-policy",
                "job_id": "test_job",
                "requested_action": "collect",
            },
            "collection_result": {"page_count": 1},
            "job_status_after": {"status": "complete"},
            "runtime_error_type": None,
            "workflow_context": {
                "GITHUB_REPOSITORY": "owner/repository",
                "GITHUB_RUN_ID": "123",
                "GITHUB_SHA": "a" * 40,
            },
            "new_raw_snapshot_count": 1,
            "new_raw_snapshots": [self.snapshot_public],
            "query_logs": [self.log_public],
            "raw_first_preserved": True,
            "credential_value_persisted": False,
        }
        self.write_audit()

    def write_audit(self):
        self.bundle.mkdir(parents=True, exist_ok=True)
        (self.bundle / "audit.json").write_text(
            json.dumps(self.audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def import_bundle(self, *, dry_run=False):
        return import_artifact(
            self.bundle,
            repository_root=self.destination_root,
            reports_root=self.destination_root / "reports/daily/collection-imports",
            dry_run=dry_run,
        )

    def test_dry_run_validates_without_writing(self):
        result = self.import_bundle(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["report"]["raw_snapshot_count"], 1)
        self.assertFalse((self.destination_root / self.snapshot_public).exists())
        self.assertIsNone(result["report_path"])
        self.assertNotIn(str(self.base), json.dumps(result["report"]))

    def test_import_is_immutable_portable_and_idempotent(self):
        first = self.import_bundle()
        destination = self.destination_root / self.snapshot_public
        self.assertEqual(destination.read_bytes(), self.snapshot.read_bytes())
        destination_log = self.destination_root / self.log_public
        self.assertEqual(len(destination_log.read_text(encoding="utf-8").splitlines()), 1)
        self.assertTrue(first["report_path"].is_file())
        report_content = first["report_path"].read_text(encoding="utf-8")
        self.assertNotIn(str(self.base), report_content)

        second = self.import_bundle()
        self.assertEqual(second["report_path"], first["report_path"])
        self.assertEqual(len(destination_log.read_text(encoding="utf-8").splitlines()), 1)

    def test_failed_collection_raw_evidence_can_be_recovered(self):
        self.audit["status"] = "failed"
        self.audit["runtime_error_type"] = "CollectionError"
        self.write_audit()
        result = self.import_bundle()
        self.assertEqual(result["report"]["collection_status"], "failed")
        self.assertTrue((self.destination_root / self.snapshot_public).is_file())

    def test_tampered_raw_snapshot_is_rejected(self):
        target = self.bundle / "repository" / self.snapshot_public
        document = json.loads(target.read_text(encoding="utf-8"))
        document["payload"]["datas"][0]["股票代码"] = "999999.SZ"
        target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ArtifactImportError, "payload hash mismatch"):
            self.import_bundle(dry_run=True)

    def test_raw_partition_mismatch_is_rejected(self):
        wrong_public = self.snapshot_public.replace("/08/09/", "/08/10/")
        wrong_target = self.bundle / "repository" / wrong_public
        wrong_target.parent.mkdir(parents=True)
        shutil.move(self.bundle / "repository" / self.snapshot_public, wrong_target)
        self.audit["new_raw_snapshots"] = [wrong_public]
        self.write_audit()
        with self.assertRaisesRegex(ArtifactImportError, "partition date mismatch"):
            self.import_bundle(dry_run=True)

    def test_preflight_and_machine_local_metadata_are_rejected(self):
        self.audit["status"] = "preflight_completed"
        self.write_audit()
        with self.assertRaisesRegex(ArtifactImportError, "Raw evidence"):
            self.import_bundle(dry_run=True)

        self.audit["status"] = "succeeded"
        self.audit["machine_path"] = "/Users/example/repository"
        self.write_audit()
        with self.assertRaisesRegex(ArtifactImportError, "machine-local path"):
            self.import_bundle(dry_run=True)

    def test_missing_raw_first_or_credential_proof_is_rejected(self):
        self.audit["raw_first_preserved"] = False
        self.write_audit()
        with self.assertRaisesRegex(ArtifactImportError, "Raw-first"):
            self.import_bundle(dry_run=True)

        self.audit["raw_first_preserved"] = True
        self.audit["credential_value_persisted"] = True
        self.write_audit()
        with self.assertRaisesRegex(ArtifactImportError, "credential exclusion"):
            self.import_bundle(dry_run=True)

    def test_duplicate_artifact_query_log_entry_is_rejected(self):
        target = self.bundle / "repository" / self.log_public
        line = target.read_text(encoding="utf-8")
        target.write_text(line + line, encoding="utf-8")
        with self.assertRaisesRegex(ArtifactImportError, "duplicate query log"):
            self.import_bundle(dry_run=True)

    def test_repository_log_conflict_is_rejected_before_raw_copy(self):
        destination_log = self.destination_root / self.log_public
        destination_log.parent.mkdir(parents=True)
        entry = json.loads(self.log.read_text(encoding="utf-8"))
        entry["record_id"] = "different"
        destination_log.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ArtifactImportError, "query log conflicts"):
            self.import_bundle()
        self.assertFalse((self.destination_root / self.snapshot_public).exists())

    def test_different_existing_raw_file_is_never_overwritten(self):
        destination = self.destination_root / self.snapshot_public
        destination.parent.mkdir(parents=True)
        destination.write_text("different", encoding="utf-8")
        with self.assertRaisesRegex(ArtifactImportError, "refusing to overwrite"):
            self.import_bundle()
        self.assertEqual(destination.read_text(encoding="utf-8"), "different")


if __name__ == "__main__":
    unittest.main()
