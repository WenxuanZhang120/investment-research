import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.repository_paths import (  # noqa: E402
    RepositoryPathError,
    repository_relative_path,
)


class RepositoryPathTests(unittest.TestCase):
    def test_serializes_repository_path_as_relative_posix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "data" / "normalized" / "example" / "manifest.json"
            self.assertEqual(
                repository_relative_path(manifest, repository_root=root),
                "data/normalized/example/manifest.json",
            )

    def test_rejects_path_outside_repository(self):
        with tempfile.TemporaryDirectory() as repository:
            with tempfile.TemporaryDirectory() as outside:
                with self.assertRaisesRegex(
                    RepositoryPathError, "outside repository root"
                ):
                    repository_relative_path(
                        Path(outside) / "manifest.json",
                        repository_root=Path(repository),
                    )


if __name__ == "__main__":
    unittest.main()
