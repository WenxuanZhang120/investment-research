import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.validate_repository import validate_repository  # noqa: E402


class ValidateRepositoryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
