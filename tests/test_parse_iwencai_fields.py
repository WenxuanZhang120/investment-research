import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.parse_iwencai_fields import (  # noqa: E402
    FieldParseError,
    load_field_mappings,
    main,
    parse_field_name,
)


class ParseIwencaiFieldsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mapping_version, cls.mappings = load_field_mappings()

    def parse(self, raw_field_name: str):
        return parse_field_name(
            raw_field_name,
            mappings=self.mappings,
            mapping_version=self.mapping_version,
        )

    def test_parses_compact_date_field(self) -> None:
        result = self.parse("收盘价[20260807]")

        self.assertEqual(result["raw_field_name"], "收盘价[20260807]")
        self.assertEqual(result["canonical_field_name"], "close")
        self.assertEqual(result["as_of_date"], "2026-08-07")
        self.assertEqual(result["context_type"], "date")
        self.assertEqual(result["mapping_status"], "mapped")
        self.assertEqual(result["confidence"], "high")

    def test_maps_ttm_valuation_field(self) -> None:
        result = self.parse("市盈率(pe,ttm)[2026-08-07]")

        self.assertEqual(result["base_field_name"], "市盈率(pe,ttm)")
        self.assertEqual(result["canonical_field_name"], "pe_ttm")
        self.assertEqual(result["as_of_date"], "2026-08-07")

    def test_maps_observed_unadjusted_close_field(self) -> None:
        result = self.parse("收盘价:不复权[20260807]")

        self.assertEqual(result["canonical_field_name"], "close")
        self.assertEqual(result["unit"], "CNY")
        self.assertEqual(result["adjustment_type"], "unadjusted")
        self.assertEqual(result["as_of_date"], "2026-08-07")

    def test_maps_observed_security_identity_fields(self) -> None:
        code = self.parse("股票代码")
        name = self.parse("股票简称")
        memberships = self.parse("股票市场类型")
        listing_date = self.parse("新股上市日期")
        listing_status = self.parse("上市状态")

        self.assertEqual(code["canonical_field_name"], "security_code")
        self.assertEqual(name["canonical_field_name"], "security_name")
        self.assertEqual(
            memberships["canonical_field_name"],
            "market_memberships",
        )
        self.assertEqual(listing_date["canonical_field_name"], "listing_date")
        self.assertEqual(listing_status["canonical_field_name"], "listing_status")
        self.assertEqual(code["mapping_status"], "mapped")
        self.assertEqual(name["mapping_status"], "mapped")

    def test_maps_observed_ohlcv_and_turnover_fields(self) -> None:
        expected = {
            "开盘价:不复权[20260807]": ("open", "CNY", "unadjusted"),
            "最高价:不复权[20260807]": ("high", "CNY", "unadjusted"),
            "最低价:不复权[20260807]": ("low", "CNY", "unadjusted"),
            "成交量[20260807]": ("volume", "shares", None),
            "成交额[20260807]": ("turnover", "CNY", None),
        }

        for raw_field_name, expectation in expected.items():
            with self.subTest(raw_field_name=raw_field_name):
                result = self.parse(raw_field_name)
                self.assertEqual(result["canonical_field_name"], expectation[0])
                self.assertEqual(result["unit"], expectation[1])
                self.assertEqual(result["adjustment_type"], expectation[2])
                self.assertEqual(result["as_of_date"], "2026-08-07")

    def test_parses_quarterly_report_period(self) -> None:
        result = self.parse("归母净利润[2026一季报]")

        self.assertEqual(result["canonical_field_name"], "net_income_parent")
        self.assertEqual(result["context_type"], "report_period")
        self.assertEqual(result["period_end"], "2026-03-31")
        self.assertEqual(result["report_type"], "2026Q1")

    def test_parses_annual_report_period(self) -> None:
        result = self.parse("归母净利润[2025年报]")

        self.assertEqual(result["period_end"], "2025-12-31")
        self.assertEqual(result["report_type"], "2025FY")

    def test_financial_compact_date_is_a_report_period(self) -> None:
        result = self.parse("营业收入[20251231]")

        self.assertEqual(result["canonical_field_name"], "revenue")
        self.assertEqual(result["context_type"], "financial_period_date")
        self.assertIsNone(result["as_of_date"])
        self.assertEqual(result["period_end"], "2025-12-31")
        self.assertEqual(result["report_type"], "2025FY")
        self.assertEqual(result["statement_type"], "income_statement")
        self.assertEqual(result["value_nature"], "duration_ytd")
        self.assertEqual(result["unit"], "CNY")

    def test_financial_metadata_uses_the_report_period_context(self) -> None:
        result = self.parse("公告日期[20260331]")

        self.assertEqual(result["canonical_field_name"], "filing_date")
        self.assertEqual(result["period_end"], "2026-03-31")
        self.assertEqual(result["report_type"], "2026Q1")

    def test_total_equity_remains_distinct_from_parent_equity(self) -> None:
        total = self.parse("所有者权益[20251231]")
        parent = self.parse("归母权益[20251231]")

        self.assertEqual(total["canonical_field_name"], "total_equity")
        self.assertEqual(parent["canonical_field_name"], "equity_parent")
        self.assertNotEqual(
            total["canonical_field_name"],
            parent["canonical_field_name"],
        )

    def test_preserves_unknown_field_without_guessing_mapping(self) -> None:
        result = self.parse("未知指标[20260807]")

        self.assertEqual(result["raw_field_name"], "未知指标[20260807]")
        self.assertEqual(result["base_field_name"], "未知指标")
        self.assertIsNone(result["canonical_field_name"])
        self.assertEqual(result["as_of_date"], "2026-08-07")
        self.assertEqual(result["mapping_status"], "unmapped")
        self.assertEqual(result["confidence"], "low")

    def test_preserves_unrecognized_context(self) -> None:
        result = self.parse("收盘价[最新]")

        self.assertEqual(result["canonical_field_name"], "close")
        self.assertEqual(result["original_context"], "最新")
        self.assertEqual(result["context_type"], "unrecognized")
        self.assertIsNone(result["as_of_date"])
        self.assertEqual(result["confidence"], "low")

    def test_rejects_invalid_calendar_date(self) -> None:
        with self.assertRaises(FieldParseError):
            self.parse("收盘价[20260230]")

    def test_command_line_entry_point_outputs_json(self) -> None:
        standard_output = io.StringIO()
        with redirect_stdout(standard_output):
            exit_code = main(
                [
                    "收盘价[20260807]",
                    "归母净利润[2026一季报]",
                    "--pretty",
                ]
            )

        self.assertEqual(exit_code, 0)
        parsed = json.loads(standard_output.getvalue())
        self.assertEqual(
            [item["canonical_field_name"] for item in parsed],
            ["close", "net_income_parent"],
        )


if __name__ == "__main__":
    unittest.main()
