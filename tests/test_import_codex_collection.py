import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.import_codex_collection import (  # noqa: E402
    CodexCollectionError,
    import_collection,
    validate_collection_artifact,
)


class ImportCodexCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifact_path = self.root / "agent-result.json"
        self.payload = {
            "success": True,
            "datas": [
                {
                    "股票代码": "000001.SZ",
                    "股票简称": "平安银行",
                    "收盘价:不复权[20260808]": 10.5,
                }
            ],
        }

    def artifact(self):
        return {
            "schema_version": 1,
            "collection_id": "daily-market-20260808",
            "dataset_kind": "market",
            "source": "iwencai",
            "query": "2026年8月8日A股测试查询",
            "as_of_date": "2026-08-08",
            "collector": {
                "method": "codex_agent",
                "tool": "hithink-market-query",
                "raw_response_unmodified": True,
            },
            "responses": [
                {
                    "fetched_at": "2026-08-08T19:00:00+08:00",
                    "raw_field_names": [
                        "收盘价:不复权[20260808]",
                        "股票代码",
                        "股票简称",
                    ],
                    "raw_response": self.payload,
                }
            ],
        }

    def write_artifact(self, value):
        self.artifact_path.write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )

    def test_import_preserves_raw_metadata_and_writes_chinese_audit(self):
        self.write_artifact(self.artifact())
        result = import_collection(
            self.artifact_path,
            repository_root=self.root,
            process=False,
        )
        self.assertEqual(result["response_count"], 1)
        self.assertTrue(result["raw_first_preserved"])
        self.assertTrue(result["audit_path"].endswith("每日采集导入审计.json"))
        snapshot = self.root / result["raw_snapshots"][0]
        envelope = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual(envelope["payload"], self.payload)
        self.assertEqual(envelope["metadata"]["as_of_date"], "2026-08-08")
        self.assertEqual(envelope["metadata"]["collection_method"], "codex_agent")
        self.assertEqual(
            envelope["metadata"]["collector_name"], "hithink-market-query"
        )
        self.assertEqual(
            envelope["metadata"]["raw_field_names"],
            sorted(self.artifact()["responses"][0]["raw_field_names"]),
        )

    def test_import_is_idempotent_for_same_raw_artifact(self):
        self.write_artifact(self.artifact())
        first = import_collection(
            self.artifact_path, repository_root=self.root, process=False
        )
        second = import_collection(
            self.artifact_path, repository_root=self.root, process=False
        )
        self.assertEqual(first["raw_snapshots"], second["raw_snapshots"])
        self.assertEqual(first["audit_path"], second["audit_path"])
        self.assertEqual(
            len(list((self.root / "data/raw/iwencai").glob("*/*/*/*.json"))),
            1,
        )

    def test_rejects_declared_fields_that_do_not_match_raw_response(self):
        artifact = self.artifact()
        artifact["responses"][0]["raw_field_names"] = ["股票代码"]
        with self.assertRaisesRegex(CodexCollectionError, "do not match"):
            validate_collection_artifact(artifact)

    def test_rejects_credential_fields_without_using_real_secrets(self):
        artifact = self.artifact()
        artifact["responses"][0]["raw_response"]["access_token"] = "dummy"
        with self.assertRaisesRegex(CodexCollectionError, "credential field"):
            validate_collection_artifact(artifact)

    def test_dry_run_writes_nothing(self):
        self.write_artifact(self.artifact())
        result = import_collection(
            self.artifact_path,
            repository_root=self.root,
            process=False,
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.root / "data").exists())
        self.assertFalse((self.root / "reports").exists())

    def test_accepts_realistic_announcement_array_shape(self):
        artifact = self.artifact()
        artifact["dataset_kind"] = "announcements"
        artifact["collector"]["tool"] = "announcement-search"
        artifact["responses"][0]["raw_response"] = {
            "status_code": 0,
            "data": [
                {
                    "title": "测试公告",
                    "publish_date": "2026-08-08",
                    "url": "https://example.invalid/announcement",
                }
            ],
        }
        artifact["responses"][0]["raw_field_names"] = [
            "publish_date",
            "title",
            "url",
        ]
        validated = validate_collection_artifact(artifact)
        self.assertEqual(validated["dataset_kind"], "announcements")


if __name__ == "__main__":
    unittest.main()
