#!/usr/bin/env python3
"""Parse dynamic iWencai field names without fetching or transforming data."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple


PARSER_VERSION = "2.0.0"
DEFAULT_MAPPING_FILE = (
    Path(__file__).resolve().parents[1] / "config" / "field_mappings.json"
)
FIELD_CONTEXT_PATTERN = re.compile(r"^(?P<base>.+)\[(?P<context>[^\[\]]+)\]$")
COMPACT_DATE_PATTERN = re.compile(r"^\d{8}$")
ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REPORT_PERIOD_PATTERN = re.compile(
    r"^(?P<year>\d{4})(?:年)?(?P<label>一季报|一季度|中报|半年报|三季报|三季度|年报)$"
)
REPORT_PERIODS = {
    "一季报": ("03-31", "Q1"),
    "一季度": ("03-31", "Q1"),
    "中报": ("06-30", "H1"),
    "半年报": ("06-30", "H1"),
    "三季报": ("09-30", "Q3"),
    "三季度": ("09-30", "Q3"),
    "年报": ("12-31", "FY"),
}
REPORT_SUFFIXES_BY_MONTH_DAY = {
    "03-31": "Q1",
    "06-30": "H1",
    "09-30": "Q3",
    "12-31": "FY",
}


class FieldParseError(ValueError):
    """Raised when a recognized field context contains invalid data."""


def load_field_mappings(
    mapping_file: Path = DEFAULT_MAPPING_FILE,
) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """Load and validate the versioned raw-to-canonical field mapping."""
    document = json.loads(mapping_file.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("field mapping root must be a JSON object")

    mapping_version = document.get("mapping_version")
    entries = document.get("fields")
    if not isinstance(mapping_version, str) or not mapping_version.strip():
        raise ValueError("mapping_version must be a non-empty string")
    if not isinstance(entries, list):
        raise ValueError("fields must be a JSON array")

    mappings: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each field mapping must be a JSON object")

        canonical_name = entry.get("canonical_field_name")
        category = entry.get("category")
        raw_base_fields = entry.get("raw_base_fields")
        if not isinstance(canonical_name, str) or not canonical_name.strip():
            raise ValueError("canonical_field_name must be a non-empty string")
        if not isinstance(category, str) or not category.strip():
            raise ValueError("category must be a non-empty string")
        if not isinstance(raw_base_fields, list) or not raw_base_fields:
            raise ValueError("raw_base_fields must be a non-empty array")

        for raw_base_field in raw_base_fields:
            if not isinstance(raw_base_field, str) or not raw_base_field.strip():
                raise ValueError("raw_base_fields entries must be non-empty strings")
            if raw_base_field in mappings:
                raise ValueError(f"duplicate raw field mapping: {raw_base_field}")
            mappings[raw_base_field] = {
                "canonical_field_name": canonical_name,
                "category": category,
                "unit": entry.get("unit"),
                "adjustment_type": entry.get("adjustment_type"),
                "statement_type": entry.get("statement_type"),
                "value_nature": entry.get("value_nature"),
            }

    return mapping_version, mappings


def _parse_date_context(context: str) -> Optional[str]:
    if COMPACT_DATE_PATTERN.fullmatch(context):
        try:
            return datetime.strptime(context, "%Y%m%d").date().isoformat()
        except ValueError as error:
            raise FieldParseError(f"invalid date context: {context}") from error

    if ISO_DATE_PATTERN.fullmatch(context):
        try:
            return datetime.strptime(context, "%Y-%m-%d").date().isoformat()
        except ValueError as error:
            raise FieldParseError(f"invalid date context: {context}") from error

    return None


def _parse_context(context: Optional[str]) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {
        "context_type": "none" if context is None else "unrecognized",
        "as_of_date": None,
        "period_end": None,
        "report_type": None,
    }
    if context is None:
        return result

    as_of_date = _parse_date_context(context)
    if as_of_date is not None:
        result["context_type"] = "date"
        result["as_of_date"] = as_of_date
        return result

    report_match = REPORT_PERIOD_PATTERN.fullmatch(context)
    if report_match:
        year = report_match.group("year")
        month_day, report_suffix = REPORT_PERIODS[report_match.group("label")]
        result["context_type"] = "report_period"
        result["period_end"] = f"{year}-{month_day}"
        result["report_type"] = f"{year}{report_suffix}"

    return result


def parse_field_name(
    raw_field_name: str,
    *,
    mappings: Dict[str, Dict[str, Any]],
    mapping_version: str,
) -> Dict[str, Any]:
    """Parse one iWencai field while preserving its exact original name."""
    if not isinstance(raw_field_name, str) or not raw_field_name.strip():
        raise FieldParseError("raw_field_name must be a non-empty string")

    field_for_parsing = raw_field_name.strip()
    context_match = FIELD_CONTEXT_PATTERN.fullmatch(field_for_parsing)
    if context_match:
        base_field_name = context_match.group("base").strip()
        original_context = context_match.group("context").strip()
    else:
        base_field_name = field_for_parsing
        original_context = None

    context = _parse_context(original_context)
    mapping = mappings.get(base_field_name)
    mapping_status = "mapped" if mapping else "unmapped"

    if (
        mapping
        and mapping["category"] in {"financial", "financial_metadata"}
        and context["context_type"] == "date"
    ):
        period_end = context["as_of_date"]
        month_day = period_end[5:] if period_end else None
        suffix = REPORT_SUFFIXES_BY_MONTH_DAY.get(month_day)
        context = {
            "context_type": "financial_period_date",
            "as_of_date": None,
            "period_end": period_end,
            "report_type": f"{period_end[:4]}{suffix}" if suffix else None,
        }

    if mapping and context["context_type"] in {
        "date",
        "report_period",
        "financial_period_date",
    }:
        confidence = "high"
    elif mapping and context["context_type"] == "none":
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "raw_field_name": raw_field_name,
        "base_field_name": base_field_name,
        "canonical_field_name": mapping["canonical_field_name"] if mapping else None,
        "category": mapping["category"] if mapping else None,
        "original_context": original_context,
        **context,
        "unit": mapping["unit"] if mapping else None,
        "adjustment_type": mapping["adjustment_type"] if mapping else None,
        "statement_type": mapping["statement_type"] if mapping else None,
        "value_nature": mapping["value_nature"] if mapping else None,
        "mapping_status": mapping_status,
        "mapping_version": mapping_version,
        "parser_version": PARSER_VERSION,
        "confidence": confidence,
    }


def parse_field_names(
    raw_field_names: Iterable[str],
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
) -> list[Dict[str, Any]]:
    mapping_version, mappings = load_field_mappings(mapping_file)
    return [
        parse_field_name(
            raw_field_name,
            mappings=mappings,
            mapping_version=mapping_version,
        )
        for raw_field_name in raw_field_names
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse dynamic iWencai field names without making network requests."
    )
    parser.add_argument("fields", nargs="+", help="One or more raw iWencai field names")
    parser.add_argument(
        "--mapping-file",
        type=Path,
        default=DEFAULT_MAPPING_FILE,
        help="Path to the versioned JSON field mapping",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        parsed_fields = parse_field_names(
            args.fields,
            mapping_file=args.mapping_file,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    json.dump(
        parsed_fields,
        sys.stdout,
        ensure_ascii=False,
        indent=2 if args.pretty else None,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
