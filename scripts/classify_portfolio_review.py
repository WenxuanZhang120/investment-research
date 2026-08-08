#!/usr/bin/env python3
"""Classify private holdings for review from explicit investor-maintained states."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = REPOSITORY_ROOT / "config" / "portfolio_review_rules.json"
CLASSIFIER_VERSION = "1.0.0"


def _load_cards(directory: Path) -> Dict[str, Dict[str, Any]]:
    cards = {}
    for path in sorted(directory.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("security_code"), str):
            raise ValueError(f"invalid investment card: {path}")
        if value["security_code"] in cards:
            raise ValueError(f"duplicate investment card security_code: {path}")
        cards[value["security_code"]] = value
    return cards


def _load_holdings(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return []
    as_of_dates = {row["as_of_date"] for row in rows}
    if len(as_of_dates) != 1:
        raise ValueError("holdings must contain one as_of_date per review")
    result = []
    for row_number, row in enumerate(rows, start=2):
        try:
            market_value = float(row["market_value"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"{path}:{row_number}: market_value is required") from error
        if not math.isfinite(market_value) or market_value < 0:
            raise ValueError(f"{path}:{row_number}: market_value must be nonnegative")
        result.append({**row, "market_value": market_value})
    return result


def _category(
    card: Optional[Dict[str, Any]],
    actual_weight: float,
    target_weight: Optional[float],
    tolerance: float,
) -> Tuple[str, List[str]]:
    if card is None:
        return "REVIEW", ["missing_investment_card"]
    if card["thesis_status"] == "broken":
        return "EXIT_candidate", ["thesis_status_broken"]
    if card["risk_status"] == "material":
        return "TRIM_candidate", ["risk_status_material"]
    if (
        card["thesis_status"] == "intact"
        and card["valuation_status"] == "attractive"
        and target_weight is not None
        and actual_weight < target_weight - tolerance
    ):
        return "ADD_candidate", ["intact_attractive_below_target"]
    if (
        card["valuation_status"] == "expensive"
        and target_weight is not None
        and actual_weight > target_weight + tolerance
    ):
        return "TRIM_candidate", ["expensive_above_target"]
    if (
        card["thesis_status"] == "intact"
        and card["risk_status"] == "stable"
        and card["valuation_status"] in {"attractive", "fair"}
    ):
        return "HOLD", ["intact_stable"]
    return "REVIEW", ["manual_judgment_required"]


def classify_portfolio(
    holdings_path: Path,
    cards_directory: Path,
    *,
    rules_path: Path = DEFAULT_RULES,
) -> Dict[str, Any]:
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    holdings = _load_holdings(holdings_path)
    cards = _load_cards(cards_directory)
    total = sum(row["market_value"] for row in holdings)
    if holdings and total <= 0:
        raise ValueError("total market_value must be positive")
    records = []
    for row in holdings:
        code = row["security_code"]
        card = cards.get(code)
        actual_weight = row["market_value"] / total
        target_weight = card.get("target_weight") if card else None
        category, reasons = _category(
            card,
            actual_weight,
            target_weight,
            rules["target_weight_tolerance"],
        )
        records.append(
            {
                "security_code": code,
                "security_name": row.get("security_name") or None,
                "as_of_date": row["as_of_date"],
                "actual_weight": round(actual_weight, 12),
                "target_weight": target_weight,
                "category": category,
                "reasons": reasons,
                "investment_card_updated_at": card.get("updated_at") if card else None,
            }
        )
    return {
        "classifier_version": CLASSIFIER_VERSION,
        "review_version": rules["review_version"],
        "records": sorted(records, key=lambda item: (item["category"], item["security_code"])),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Classify private holdings for review.")
    parser.add_argument("--holdings", required=True, type=Path)
    parser.add_argument("--cards", required=True, type=Path)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = classify_portfolio(args.holdings, args.cards, rules_path=args.rules)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
