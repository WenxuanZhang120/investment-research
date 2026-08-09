import json
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPOSITORY_ROOT / "config/codex_daily_collection.json"


class CodexDailyCollectionConfigTests(unittest.TestCase):
    def test_plan_is_chinese_friendly_and_preserves_boundaries(self):
        plan = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(plan["collector"], "codex_agent")
        self.assertTrue(plan["display_name"].startswith("Codex 每日"))
        self.assertTrue(plan["principles"]["save_raw_before_processing"])
        self.assertTrue(plan["principles"]["raw_response_must_be_unmodified"])
        self.assertFalse(plan["principles"]["guess_unknown_fields"])
        self.assertFalse(plan["principles"]["investment_judgment_allowed"])
        self.assertFalse(plan["principles"]["automatic_trading_allowed"])
        task_ids = {item["task_id"] for item in plan["tasks"]}
        self.assertEqual(
            task_ids,
            {
                "daily_a_share_market",
                "recent_announcements",
                "recent_company_news",
                "financial_plan_follow_up",
            },
        )
        self.assertTrue(all(item["display_name"] for item in plan["tasks"]))


if __name__ == "__main__":
    unittest.main()
