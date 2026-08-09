import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.screen_market_research_queue import (  # noqa: E402
    CONNECTOR_DIRECTORY,
    CONNECTOR_MAX_FILE_BYTES,
    _percentile_scores,
    build_screen,
    write_screen,
)


class ScreenMarketResearchQueueTests(unittest.TestCase):
    def test_percentile_direction_is_explicit(self):
        values = {"A": 1.0, "B": 2.0, "C": 3.0}
        self.assertEqual(_percentile_scores(values, "higher")["C"], 1.0)
        self.assertEqual(_percentile_scores(values, "lower")["A"], 1.0)

    def test_real_full_market_inputs_build_point_in_time_queue(self):
        market = REPOSITORY_ROOT / "data/normalized/runs/iwencai/2026/08/08/39fd6c21403eedd2e816/manifest.json"
        metrics = REPOSITORY_ROOT / "data/derived/runs/iwencai/2026/08/08/c851f3cdfa64076c512b/manifest.json"
        built = build_screen(market, metrics)
        self.assertEqual(built["coverage"]["universe_count"], 3193)
        self.assertEqual(sum(built["coverage"]["priority_counts"].values()), 3193)
        self.assertTrue(
            all(
                not record["security_code"].startswith(
                    ("300", "301", "302", "688", "689")
                )
                and not record["security_code"].endswith(".BJ")
                for record in built["records"]
            )
        )
        eligible = [x for x in built["records"] if x["eligible"]]
        self.assertTrue(eligible)
        self.assertTrue(
            all(x["financial_available_from"] <= x["as_of_date"] for x in eligible)
        )

    def test_writes_hashed_immutable_bundle(self):
        market = REPOSITORY_ROOT / "data/normalized/runs/iwencai/2026/08/08/39fd6c21403eedd2e816/manifest.json"
        metrics = REPOSITORY_ROOT / "data/derived/runs/iwencai/2026/08/08/c851f3cdfa64076c512b/manifest.json"
        built = build_screen(market, metrics)
        with tempfile.TemporaryDirectory() as temporary:
            destination = write_screen(built, derived_root=Path(temporary))
            manifest = json.loads((destination / "manifest.json").read_text())
            content = (destination / manifest["table"]["file"]).read_bytes()
            self.assertEqual(manifest["table"]["sha256"], hashlib.sha256(content).hexdigest())
            connector = destination / CONNECTOR_DIRECTORY
            connector_manifest = json.loads(
                (connector / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(connector_manifest["source_manifest"], "../manifest.json")
            self.assertEqual(
                connector_manifest["source_table"]["sha256"],
                manifest["table"]["sha256"],
            )
            summary = connector_manifest["tables"]["p0_p1_summary"]
            summary_content = (connector / summary["file"]).read_bytes()
            self.assertLessEqual(len(summary_content), CONNECTOR_MAX_FILE_BYTES)
            self.assertEqual(summary["sha256"], hashlib.sha256(summary_content).hexdigest())
            self.assertTrue(
                all(
                    json.loads(line)["priority"] in {"P0", "P1"}
                    for line in summary_content.decode("utf-8").splitlines()
                )
            )
            partition_content = b""
            for partition in connector_manifest["tables"]["full_queue"]["partitions"]:
                chunk = (connector / partition["file"]).read_bytes()
                self.assertLessEqual(len(chunk), CONNECTOR_MAX_FILE_BYTES)
                self.assertEqual(partition["sha256"], hashlib.sha256(chunk).hexdigest())
                partition_content += chunk
            self.assertEqual(partition_content, content)
            with self.assertRaises(FileExistsError):
                write_screen(built, derived_root=Path(temporary))


if __name__ == "__main__":
    unittest.main()
