import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.resolve_monitoring_collection_scope import (  # noqa: E402
    MonitoringScopeError,
    latest_p0_targets,
    resolve_monitoring_jobs,
)


class ResolveMonitoringCollectionScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        shutil.copy2(
            REPOSITORY_ROOT / "config/investment_universe.json",
            self.root / "config/investment_universe.json",
        )
        self.manifest = self.write_screening_bundle()
        self.plan = self.root / "config/plan.json"
        self.plan.write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "task_id": "p0_repurchase_announcements",
                            "dataset_kind": "announcements",
                            "tool": "announcement-search",
                            "scope_type": "latest_p0",
                            "priority": "P0",
                            "target_batch_size": 25,
                            "maximum_target_count": 50,
                            "allowed_event_types": ["share_repurchase"],
                            "query_template": "P0回购｜标的：{p0_security_list}",
                        },
                        {
                            "task_id": "china_macro_policy_news",
                            "dataset_kind": "news",
                            "tool": "news-search",
                            "scope_type": "market_wide",
                            "query_template": "中国宏观政策",
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def write_screening_bundle(self):
        directory = self.root / "data/derived/runs/screening/2026/08/08/dummy"
        directory.mkdir(parents=True)
        records = [
            {
                "security_code": "600001.SH",
                "security_name": "甲公司",
                "as_of_date": "2026-08-08",
                "priority": "P0",
            },
            {
                "security_code": "000002.SZ",
                "security_name": "乙公司",
                "as_of_date": "2026-08-08",
                "priority": "P0",
            },
            {
                "security_code": "600003.SH",
                "security_name": "丙公司",
                "as_of_date": "2026-08-08",
                "priority": "P1",
            },
        ]
        content = (
            "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n"
        ).encode()
        (directory / "queue.jsonl").write_bytes(content)
        manifest = directory / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "universe_id": "cn_sh_sz_main_board_a",
                    "table": {
                        "logical_name": "market_research_queue",
                        "file": "queue.jsonl",
                        "record_count": 3,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    },
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def test_resolves_only_p0_companies_and_keeps_macro_market_wide(self):
        result = resolve_monitoring_jobs(
            plan_path=self.plan,
            derived_root=self.root / "data/derived",
            universe_path=self.root / "config/investment_universe.json",
            repository_root=self.root,
            run_date="2026-08-09",
        )
        self.assertEqual(result["p0_target_count"], 2)
        self.assertEqual(result["job_count"], 2)
        p0_job, macro_job = result["jobs"]
        self.assertIn("600001.SH 甲公司", p0_job["query"])
        self.assertNotIn("丙公司", p0_job["query"])
        self.assertEqual(
            p0_job["collection_scope"]["target_security_codes"],
            ["000002.SZ", "600001.SH"],
        )
        self.assertEqual(
            p0_job["collection_scope"]["allowed_event_types"],
            ["share_repurchase"],
        )
        self.assertEqual(macro_job["collection_scope"]["scope_type"], "market_wide")

    def test_rejects_tampered_screening_queue(self):
        queue = self.manifest.parent / "queue.jsonl"
        queue.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(MonitoringScopeError, "hash mismatch"):
            latest_p0_targets(
                derived_root=self.root / "data/derived",
                universe_path=self.root / "config/investment_universe.json",
                repository_root=self.root,
            )


if __name__ == "__main__":
    unittest.main()
