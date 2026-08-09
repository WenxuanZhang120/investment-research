import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.investment_universe import (  # noqa: E402
    etf_index_family,
    load_investment_universe,
    stock_code_allowed,
    stock_record_allowed,
)


class InvestmentUniverseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.universe = load_investment_universe()

    def test_accepts_shenzhen_and_shanghai_main_board_including_st(self):
        self.assertTrue(stock_code_allowed("600000.SH", self.universe))
        self.assertTrue(stock_record_allowed(
            {
                "security_code": "002001.SZ",
                "listing_status": "ST",
                "market_memberships": ["沪深A股"],
            },
            self.universe,
        ))

    def test_rejects_untradeable_boards_by_exchange_prefix_and_membership(self):
        for code in (
            "300001.SZ",
            "301001.SZ",
            "302132.SZ",
            "688001.SH",
            "689001.SH",
            "430001.BJ",
        ):
            with self.subTest(code=code):
                self.assertFalse(stock_code_allowed(code, self.universe))
        self.assertFalse(stock_record_allowed(
            {
                "security_code": "600000.SH",
                "listing_status": "正常上市",
                "market_memberships": ["科创板"],
            },
            self.universe,
        ))

    def test_recognizes_only_configured_etf_index_families(self):
        self.assertEqual(
            etf_index_family("纳斯达克100指数", self.universe), "nasdaq_100"
        )
        self.assertEqual(etf_index_family("S&P 500", self.universe), "sp_500")
        self.assertIsNone(etf_index_family("沪深300", self.universe))


if __name__ == "__main__":
    unittest.main()
