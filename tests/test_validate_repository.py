import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.validate_repository import validate_repository  # noqa: E402


class ValidateRepositoryTests(unittest.TestCase):
    @staticmethod
    def empty_repository(root):
        (root / "data/raw/_query_log").mkdir(parents=True)
        (root / "data/normalized").mkdir(parents=True)
        (root / "data/derived").mkdir(parents=True)
        (root / "config").mkdir()

    def test_current_repository_integrity(self):
        self.assertEqual(validate_repository(REPOSITORY_ROOT), [])

    def test_detects_tampered_raw_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
            (root / "data/raw/_query_log").mkdir(parents=True)
            (root / "data/normalized").mkdir(parents=True)
            (root / "data/derived").mkdir(parents=True)
            (root / "config").mkdir()
            errors = validate_repository(root)
            self.assertTrue(any("payload hash mismatch" in error for error in errors))

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
