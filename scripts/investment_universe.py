#!/usr/bin/env python3
"""Validate and apply the versioned investable-universe boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNIVERSE = REPOSITORY_ROOT / "config" / "investment_universe.json"
LISTED_CODE_PATTERN = re.compile(r"^(?P<code>\d{6})\.(?P<exchange>SH|SZ|BJ)$")


class InvestmentUniverseError(ValueError):
    """Raised when the configured investment universe is ambiguous."""


def _string_list(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise InvestmentUniverseError(f"{label} must contain unique strings")
    return list(value)


def load_investment_universe(path: Path = DEFAULT_UNIVERSE) -> Dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise InvestmentUniverseError("investment universe schema_version must be 1")
    if not isinstance(value.get("universe_version"), str):
        raise InvestmentUniverseError("universe_version must be a string")
    stocks = value.get("stocks")
    etfs = value.get("etfs")
    if not isinstance(stocks, dict) or not isinstance(etfs, dict):
        raise InvestmentUniverseError("stocks and etfs must be objects")
    for section, key in ((stocks, "universe_id"), (etfs, "universe_id")):
        if not isinstance(section.get(key), str) or not section[key]:
            raise InvestmentUniverseError(f"{key} must be a non-empty string")
    stocks["allowed_exchanges"] = _string_list(
        stocks.get("allowed_exchanges"), "stocks.allowed_exchanges"
    )
    stocks["allowed_listing_statuses"] = _string_list(
        stocks.get("allowed_listing_statuses"), "stocks.allowed_listing_statuses"
    )
    stocks["excluded_board_memberships"] = _string_list(
        stocks.get("excluded_board_memberships"),
        "stocks.excluded_board_memberships",
    )
    stocks["excluded_code_prefixes"] = _string_list(
        stocks.get("excluded_code_prefixes"), "stocks.excluded_code_prefixes"
    )
    if not isinstance(stocks.get("minimum_expected_count"), int):
        raise InvestmentUniverseError("stocks.minimum_expected_count must be an integer")
    if stocks["minimum_expected_count"] <= 0:
        raise InvestmentUniverseError(
            "stocks.minimum_expected_count must be greater than zero"
        )
    etfs["allowed_exchanges"] = _string_list(
        etfs.get("allowed_exchanges"), "etfs.allowed_exchanges"
    )
    if not isinstance(etfs.get("required_fund_type"), str) or not etfs[
        "required_fund_type"
    ]:
        raise InvestmentUniverseError(
            "etfs.required_fund_type must be a non-empty string"
        )
    for flag in ("include_domestic_cross_border_qdii", "include_overseas_listed_funds"):
        if not isinstance(etfs.get(flag), bool):
            raise InvestmentUniverseError(f"etfs.{flag} must be boolean")
    families = etfs.get("tracked_index_families")
    if not isinstance(families, dict) or not families:
        raise InvestmentUniverseError("etfs.tracked_index_families must be an object")
    for family, aliases in families.items():
        if not isinstance(family, str) or not family:
            raise InvestmentUniverseError("ETF index family names must be strings")
        families[family] = _string_list(aliases, f"ETF aliases for {family}")
    return value


def stock_code_allowed(code: Any, universe: Dict[str, Any]) -> bool:
    if not isinstance(code, str):
        return False
    match = LISTED_CODE_PATTERN.fullmatch(code)
    if match is None:
        return False
    rules = universe["stocks"]
    if match.group("exchange") not in rules["allowed_exchanges"]:
        return False
    return not any(
        match.group("code").startswith(prefix)
        for prefix in rules["excluded_code_prefixes"]
    )


def stock_record_allowed(record: Dict[str, Any], universe: Dict[str, Any]) -> bool:
    if not stock_code_allowed(record.get("security_code"), universe):
        return False
    memberships = record.get("market_memberships")
    if isinstance(memberships, list):
        excluded = set(universe["stocks"]["excluded_board_memberships"])
        if excluded.intersection(item for item in memberships if isinstance(item, str)):
            return False
    status = record.get("listing_status")
    return status is None or status in universe["stocks"]["allowed_listing_statuses"]


def normalized_index_name(value: str) -> str:
    return re.sub(r"[\s&＆._\-—－]", "", value).upper()


def etf_index_family(
    tracked_index: Any,
    universe: Dict[str, Any],
) -> Optional[str]:
    if not isinstance(tracked_index, str) or not tracked_index.strip():
        return None
    normalized = normalized_index_name(tracked_index)
    for family, aliases in universe["etfs"]["tracked_index_families"].items():
        if any(normalized_index_name(alias) in normalized for alias in aliases):
            return family
    return None


def allowed_stock_codes(
    records: Iterable[Dict[str, Any]], universe: Dict[str, Any]
) -> set[str]:
    return {
        record["security_code"]
        for record in records
        if stock_record_allowed(record, universe)
    }
