#!/usr/bin/env python3
"""Validate private portfolio inputs without echoing sensitive row values."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = REPOSITORY_ROOT / "config" / "portfolio_schema.json"


def _schema(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("portfolio schema root must be an object")
    return value


def _date_valid(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _number(value: str) -> Optional[float]:
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def validate_csv(path: Path, definition: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != definition["columns"]:
            return [f"{path}: header does not match the versioned schema"]
        for row_number, row in enumerate(reader, start=2):
            for field in definition["required"]:
                if not (row.get(field) or "").strip():
                    errors.append(f"{path}:{row_number}: {field} is required")
            for field in definition.get("date_fields", []):
                value = (row.get(field) or "").strip()
                if value and not _date_valid(value):
                    errors.append(f"{path}:{row_number}: {field} must be YYYY-MM-DD")
            for field in definition.get("nonnegative_number_fields", []):
                value = (row.get(field) or "").strip()
                if not value:
                    continue
                parsed = _number(value)
                if parsed is None or parsed < 0:
                    errors.append(f"{path}:{row_number}: {field} must be nonnegative")
            for field in definition.get("ratio_fields", []):
                value = (row.get(field) or "").strip()
                if not value:
                    continue
                parsed = _number(value)
                if parsed is None or not 0 <= parsed <= 1:
                    errors.append(f"{path}:{row_number}: {field} must be between 0 and 1")
            for field, allowed in definition.get("enum_fields", {}).items():
                value = (row.get(field) or "").strip()
                if value and value not in allowed:
                    errors.append(f"{path}:{row_number}: {field} is not an allowed value")
    return errors


def validate_card(path: Path, definition: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [f"{path}: invalid JSON"]
    if not isinstance(card, dict):
        return [f"{path}: card root must be an object"]
    for field in definition["required"]:
        if field not in card:
            errors.append(f"{path}: {field} is required")
    if card.get("status") not in definition["status_values"]:
        errors.append(f"{path}: status is not an allowed value")
    for field in definition["array_fields"]:
        if field in card and not isinstance(card[field], list):
            errors.append(f"{path}: {field} must be an array")
    updated_at = card.get("updated_at")
    if updated_at:
        try:
            datetime.fromisoformat(updated_at)
        except (TypeError, ValueError):
            errors.append(f"{path}: updated_at must be ISO 8601")
    target = card.get("target_weight")
    if target is not None and (
        not isinstance(target, (int, float))
        or isinstance(target, bool)
        or not math.isfinite(target)
        or not 0 <= target <= 1
    ):
        errors.append(f"{path}: target_weight must be null or between 0 and 1")
    return errors


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate private portfolio files.")
    parser.add_argument("--holdings", type=Path)
    parser.add_argument("--transactions", type=Path)
    parser.add_argument("--cards", type=Path)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args(argv)
    if not any((args.holdings, args.transactions, args.cards)):
        parser.error("provide at least one portfolio input")
    try:
        schema = _schema(args.schema)
        errors: List[str] = []
        checked = 0
        for path, name in ((args.holdings, "holdings"), (args.transactions, "transactions")):
            if path:
                checked += 1
                errors.extend(validate_csv(path, schema[name]))
        if args.cards:
            if not args.cards.is_dir():
                errors.append(f"{args.cards}: cards path must be a directory")
            else:
                card_paths = sorted(args.cards.glob("*.json"))
                checked += len(card_paths)
                for path in card_paths:
                    errors.extend(validate_card(path, schema["investment_card"]))
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"validated {checked} portfolio file(s); no row values were displayed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
