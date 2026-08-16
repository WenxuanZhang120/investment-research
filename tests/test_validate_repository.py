import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.validate_repository import (  # noqa: E402
    GITHUB_FILE_LIMIT,
    validate_repository,
)


class ValidateRepositoryTests(unittest.TestCase):
    @staticmethod
    def empty_repository(root):
        subprocess.run(
            ["git", "init", "--quiet", str(root)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        (root / "data/raw/_query_log").mkdir(parents=True)
        (root / "data/normalized").mkdir(parents=True)
        (root / "data/derived").mkdir(parents=True)
        (root / "config").mkdir()

    @staticmethod
    def write_file_at_github_limit(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.truncate(GITHUB_FILE_LIMIT)

    def test_empty_temporary_repository_integrity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            self.assertEqual(validate_repository(root), [])

    def test_file_size_check_fails_closed_outside_git_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data/raw/_query_log").mkdir(parents=True)
            (root / "data/normalized").mkdir(parents=True)
            (root / "data/derived").mkdir(parents=True)
            (root / "config").mkdir()

            self.assertIn(
                "cannot enumerate Git publish candidates",
                validate_repository(root),
            )

    def test_file_size_check_excludes_gitignored_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            (root / ".gitignore").write_text(
                "web/.next/\n.pnpm-store/\n",
                encoding="utf-8",
            )
            self.write_file_at_github_limit(
                root / "web/.next/cache/webpack/server-production/4.pack"
            )
            self.write_file_at_github_limit(root / ".pnpm-store/store.pack")

            self.assertEqual(validate_repository(root), [])

    def test_file_size_check_rejects_tracked_file_even_when_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            artifact = root / "tracked-large.bin"
            artifact.write_bytes(b"tracked before ignore\n")
            subprocess.run(
                ["git", "-C", str(root), "add", artifact.name],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            (root / ".gitignore").write_text(
                f"{artifact.name}\n",
                encoding="utf-8",
            )
            self.write_file_at_github_limit(artifact)

            errors = validate_repository(root)

            self.assertTrue(
                any(
                    error.startswith(f"{artifact.name}:")
                    and "exceeds GitHub 100 MiB file limit" in error
                    for error in errors
                )
            )

    def test_file_size_check_rejects_nonignored_untracked_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            artifact = root / "untracked-large.bin"
            self.write_file_at_github_limit(artifact)

            errors = validate_repository(root)

            self.assertTrue(
                any(
                    error.startswith(f"{artifact.name}:")
                    and "exceeds GitHub 100 MiB file limit" in error
                    for error in errors
                )
            )

    def test_detects_tampered_raw_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            path = root / "data/raw/iwencai/2026/08/08/snapshot.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "record_id": "x",
                            "payload_sha256": "not-the-real-hash",
                        },
                        "payload": {"value": 1},
                    }
                ),
                encoding="utf-8",
            )
            errors = validate_repository(root)
            self.assertTrue(any("payload hash mismatch" in error for error in errors))

    def test_detects_connector_parts_that_do_not_reconstruct_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            bundle = root / "data/derived/runs/screening/2026/08/07/example"
            connector = bundle / "github_connector"
            connector.mkdir(parents=True)
            source_content = b'{"priority":"P0"}\n'
            source_sha = hashlib.sha256(source_content).hexdigest()
            (bundle / "queue.jsonl").write_bytes(source_content)
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "table": {
                            "logical_name": "market_research_queue",
                            "file": "queue.jsonl",
                            "record_count": 1,
                            "sha256": source_sha,
                        }
                    }
                ),
                encoding="utf-8",
            )
            wrong_content = b'{"priority":"P1"}\n'
            wrong_sha = hashlib.sha256(wrong_content).hexdigest()
            (connector / "summary.jsonl").write_bytes(source_content)
            (connector / "part-0001.jsonl").write_bytes(wrong_content)
            (connector / "manifest.json").write_text(
                json.dumps(
                    {
                        "kind": "github_connector_export",
                        "max_file_size_bytes": 900 * 1024,
                        "source_manifest": "../manifest.json",
                        "source_table": {
                            "file": "queue.jsonl",
                            "record_count": 1,
                            "sha256": source_sha,
                        },
                        "tables": {
                            "p0_p1_summary": {
                                "file": "summary.jsonl",
                                "record_count": 1,
                                "priority_counts": {"P0": 1, "P1": 0},
                                "byte_size": len(source_content),
                                "sha256": source_sha,
                            },
                            "full_queue": {
                                "record_count": 1,
                                "partitions": [
                                    {
                                        "file": "part-0001.jsonl",
                                        "record_count": 1,
                                        "byte_size": len(wrong_content),
                                        "sha256": wrong_sha,
                                    }
                                ],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            errors = validate_repository(root, tracked_paths=[])
            self.assertTrue(
                any("do not reconstruct source queue" in error for error in errors)
            )

    def test_detects_local_absolute_paths_in_public_artifacts(self):
        examples = (
            "/Users/example/project/data/manifest.json",
            "/home/example/project/data/manifest.json",
            "C:\\Users\\example\\project\\data\\manifest.json",
        )
        for index, leaked_path in enumerate(examples):
            with self.subTest(leaked_path=leaked_path):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    self.empty_repository(root)
                    artifact = root / "reports/daily" / f"leak-{index}.json"
                    artifact.parent.mkdir(parents=True)
                    artifact.write_text(
                        json.dumps({"manifest": leaked_path}),
                        encoding="utf-8",
                    )
                    relative = artifact.relative_to(root)
                    errors = validate_repository(root, tracked_paths=[relative])
                    self.assertTrue(
                        any("machine-local absolute path" in error for error in errors)
                    )

    def test_detects_local_path_in_nonignored_untracked_public_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            artifact = root / "reports/daily/untracked-leak.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                json.dumps({"manifest": "/Users/example/private/manifest.json"}),
                encoding="utf-8",
            )

            errors = validate_repository(root)

            self.assertTrue(
                any(
                    error.startswith("reports/daily/untracked-leak.json:")
                    and "machine-local absolute path" in error
                    for error in errors
                )
            )

    def test_ignores_local_path_in_gitignored_untracked_public_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            (root / ".gitignore").write_text(
                "reports/daily/ignored-leak.json\n",
                encoding="utf-8",
            )
            artifact = root / "reports/daily/ignored-leak.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                json.dumps({"manifest": "/Users/example/private/manifest.json"}),
                encoding="utf-8",
            )

            self.assertEqual(validate_repository(root), [])

    def test_detects_local_path_in_tracked_artifact_even_when_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            artifact = root / "reports/daily/tracked-leak.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                json.dumps({"manifest": "/Users/example/private/manifest.json"}),
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(root), "add", artifact.relative_to(root).as_posix()],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            (root / ".gitignore").write_text(
                "reports/daily/tracked-leak.json\n",
                encoding="utf-8",
            )

            errors = validate_repository(root)

            self.assertTrue(
                any(
                    error.startswith("reports/daily/tracked-leak.json:")
                    and "machine-local absolute path" in error
                    for error in errors
                )
            )

    def test_detects_tracked_private_paths_and_filenames(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            holdings = root / "portfolio/private/holdings.csv"
            private_json = root / "reports/something.private.json"
            holdings.parent.mkdir(parents=True)
            private_json.parent.mkdir(parents=True)
            holdings.write_text("security_code\n", encoding="utf-8")
            private_json.write_text("{}\n", encoding="utf-8")
            errors = validate_repository(
                root,
                tracked_paths=[
                    holdings.relative_to(root),
                    private_json.relative_to(root),
                ],
            )
            self.assertTrue(any("portfolio/private/holdings.csv" in x for x in errors))
            self.assertTrue(any("something.private.json" in x for x in errors))

    def test_detects_sensitive_key_in_nonignored_untracked_raw_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            payload = {"rows": [], "metadata": {"token": "test-only-marker"}}
            digest = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            artifact = (
                root
                / "data/raw/iwencai/2026/08/16/sensitive-snapshot.json"
            )
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "record_id": "test-record",
                            "payload_sha256": digest,
                        },
                        "payload": payload,
                    }
                ),
                encoding="utf-8",
            )

            errors = validate_repository(root)

            self.assertTrue(
                any(
                    error.startswith(
                        "data/raw/iwencai/2026/08/16/sensitive-snapshot.json:"
                    )
                    and "forbidden credential field" in error
                    for error in errors
                )
            )

    def test_detects_bearer_value_in_nonignored_untracked_jsonl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            artifact = root / "reports/daily/sensitive.jsonl"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                json.dumps(
                    {
                        "diagnostic": "Bearer test_only_opaque_token_123456789"
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            errors = validate_repository(root)

            self.assertTrue(
                any(
                    error.startswith("reports/daily/sensitive.jsonl:1:")
                    and "Bearer credential value" in error
                    for error in errors
                )
            )

    def test_detects_sensitive_name_declared_in_raw_field_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            artifact = root / "reports/daily/schema-leak.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                json.dumps(
                    {
                        "raw_field_names": ["股票代码", "Authorization"],
                        "payload": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            errors = validate_repository(root)

            self.assertTrue(
                any(
                    error.startswith("reports/daily/schema-leak.json:")
                    and "declares a forbidden credential field" in error
                    for error in errors
                )
            )

    def test_detects_account_identifier_header_in_public_csv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            artifact = root / "portfolio/public/holdings.csv"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                "account_id,security_code,quantity\nredacted,000001.SZ,10\n",
                encoding="utf-8",
            )

            errors = validate_repository(root)

            self.assertTrue(
                any(
                    error.startswith("portfolio/public/holdings.csv:")
                    and "personal/account identifier field" in error
                    for error in errors
                )
            )

    def test_does_not_scan_gitignored_sensitive_structured_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            (root / ".gitignore").write_text(
                "reports/daily/ignored-sensitive.json\n",
                encoding="utf-8",
            )
            artifact = root / "reports/daily/ignored-sensitive.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                json.dumps({"Authorization": "test-only-marker"}),
                encoding="utf-8",
            )

            self.assertEqual(validate_repository(root), [])

    def test_detects_collection_budget_above_safe_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.empty_repository(root)
            source_config = REPOSITORY_ROOT / "config"
            for name in (
                "investment_universe.json",
                "collection_budget.json",
                "codex_daily_collection.json",
                "system_completion_requirements.json",
            ):
                (root / "config" / name).write_text(
                    (source_config / name).read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            budget_path = root / "config/collection_budget.json"
            budget = json.loads(budget_path.read_text(encoding="utf-8"))
            budget["trading_day"]["financial_max_pages"] = 100
            budget_path.write_text(json.dumps(budget), encoding="utf-8")
            errors = validate_repository(root, tracked_paths=[])
            self.assertTrue(any("exceed daily safe limit" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
