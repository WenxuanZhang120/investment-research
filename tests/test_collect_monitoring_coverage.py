import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.collect_monitoring_coverage import (  # noqa: E402
    assess_coverage,
    collect_monitoring_coverage,
    summarize_payload,
)


class CollectMonitoringCoverageTests(unittest.TestCase):
    def test_summary_detects_truncation_and_off_scope_items(self):
        payload = {
            "status_code": 0,
            "total": 3,
            "data": [
                {
                    "title": "甲公司新闻",
                    "url": "https://official.test/a",
                    "publish_time": "2026-08-09T10:00:00+08:00",
                    "stock_infos": [{"code": "600001", "name": "甲公司"}],
                    "extra": {"real_publish_source": "测试媒体"},
                },
                {
                    "title": "乙公司新闻",
                    "url": "https://media.test/b",
                    "publish_time": "2026-08-09T11:00:00+08:00",
                    "stock_infos": [{"code": "600002", "name": "乙公司"}],
                },
            ],
        }
        result = summarize_payload(
            payload,
            requested_size=2,
            scope={
                "scope_type": "p0_securities",
                "target_security_codes": ["600001.SH"],
            },
        )
        self.assertTrue(result["truncated_within_reported_total"])
        self.assertEqual(result["matched_target_items"], 1)
        self.assertEqual(result["off_scope_items"], 1)
        self.assertEqual(result["publishers"], ["测试媒体"])

    def test_coverage_assessment_does_not_confuse_connection_with_completeness(self):
        result = assess_coverage(
            [
                {
                    "task_id": "p0_company_news",
                    "status": "succeeded",
                    "returned_count": 2,
                    "reported_total": 9,
                    "truncated_within_reported_total": True,
                    "collection_scope": {"scope_type": "p0_securities"},
                    "unidentified_security_items": 2,
                    "matched_target_items": 0,
                }
            ]
        )
        self.assertEqual(result["coverage_status"], "insufficient")
        self.assertFalse(result["reported_result_coverage_complete"])
        self.assertEqual(len(result["coverage_gaps"]), 3)

    def test_runner_calls_skill_script_and_saves_scoped_raw(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data/raw").mkdir(parents=True)
            (root / "reports/daily").mkdir(parents=True)
            fake = root / "fake_skill.py"
            fake.write_text(
                "import json,sys\n"
                "output=sys.argv[sys.argv.index('--output')+1]\n"
                "json.dump({'status_code':0,'total':1,'data':["
                "{'title':'政策新闻','url':'https://example.test/a',"
                "'publish_time':'2026-08-09T10:00:00+08:00'}]},"
                "open(output,'w'))\n",
                encoding="utf-8",
            )
            jobs = [
                {
                    "task_id": "china_macro_policy_news",
                    "collection_id": "china_macro_policy_news-2026-08-09",
                    "dataset_kind": "news",
                    "tool": "news-search",
                    "as_of_date": "2026-08-09",
                    "requested_result_count": 20,
                    "query": "中国宏观政策",
                    "collection_scope": {
                        "scope_schema_version": 1,
                        "scope_type": "market_wide",
                        "topic_id": "china_macro_policy_news",
                    },
                }
            ]
            result = collect_monitoring_coverage(
                news_script=fake,
                announcement_script=fake,
                repository_root=root,
                run_date="2026-08-09",
                jobs=jobs,
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["coverage_status"], "reported_results_complete")
            snapshot = root / result["results"][0]["raw_snapshot"]
            envelope = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual(
                envelope["metadata"]["collection_scope"]["topic_id"],
                "china_macro_policy_news",
            )
            self.assertNotIn("IWENCAI_API_KEY", snapshot.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
