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
                "nasdaq_sp500_etfs",
                "p0_repurchase_announcements",
                "p0_company_news",
                "china_macro_policy_news",
                "global_macro_market_news",
                "industry_policy_news",
                "financial_plan_follow_up",
            },
        )
        self.assertTrue(all(item["display_name"] for item in plan["tasks"]))
        by_id = {item["task_id"]: item for item in plan["tasks"]}
        self.assertEqual(by_id["p0_company_news"]["scope_type"], "latest_p0")
        self.assertEqual(
            by_id["p0_repurchase_announcements"]["allowed_event_types"],
            ["share_repurchase"],
        )
        for task_id in (
            "china_macro_policy_news",
            "global_macro_market_news",
            "industry_policy_news",
        ):
            self.assertEqual(by_id[task_id]["scope_type"], "market_wide")


if __name__ == "__main__":
    unittest.main()
