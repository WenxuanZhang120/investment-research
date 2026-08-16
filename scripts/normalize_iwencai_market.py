#!/usr/bin/env python3
"""Normalize saved iWencai responses into core market-data tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.audit_iwencai_response import extract_table_components  # noqa: E402
from scripts.investment_universe import (  # noqa: E402
    DEFAULT_UNIVERSE,
    load_investment_universe,
    stock_record_allowed,
)
from scripts.parse_iwencai_fields import (  # noqa: E402
    DEFAULT_MAPPING_FILE,
    load_field_mappings,
    parse_field_name,
)
from scripts.repository_paths import repository_relative_path  # noqa: E402


NORMALIZER_VERSION = "2.2.0"
RECORD_SCHEMA_VERSION = 2
BUNDLE_SCHEMA_VERSION = 2
DEFAULT_NORMALIZED_ROOT = REPOSITORY_ROOT / "data" / "normalized"
PROJECT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
SECURITY_CODE_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
COMPACT_DATE_PATTERN = re.compile(r"^\d{8}$")
TABLE_FILES = {
    "security_master": "security_master.jsonl",
    "market_bars_daily": "market_bars_daily.jsonl",
    "valuation_snapshots": "valuation_snapshots.jsonl",
}
PRIMARY_KEYS = {
    "security_master": ["security_code", "observed_date"],
    "market_bars_daily": ["security_code", "trade_date", "adjustment_type"],
    "valuation_snapshots": ["security_code", "as_of_date"],
}


class NormalizationError(ValueError):
    """Raised when raw responses cannot be normalized without guessing."""


def _canonical_payload(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise NormalizationError("raw metadata fetched_at must be an ISO 8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise NormalizationError("raw metadata fetched_at is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise NormalizationError("raw metadata fetched_at must include a timezone")
    return timestamp.astimezone(PROJECT_TIMEZONE)


def _load_envelope(snapshot_path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    document = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise NormalizationError("raw snapshot root must be a JSON object")
    metadata = document.get("metadata")
    payload = document.get("payload")
    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        raise NormalizationError("raw snapshot must contain metadata and object payload")
    if metadata.get("source") != "iwencai":
        raise NormalizationError("raw snapshot source must be iwencai")

    expected_hash = metadata.get("payload_sha256")
    actual_hash = hashlib.sha256(_canonical_payload(payload)).hexdigest()
    if not isinstance(expected_hash, str) or expected_hash != actual_hash:
        raise NormalizationError("raw payload checksum does not match metadata")

    record_id = metadata.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        raise NormalizationError("raw metadata record_id must be a non-empty string")
    _parse_timestamp(metadata.get("fetched_at"))
    return metadata, payload


def _reported_total(meta: Dict[str, Any]) -> Optional[int]:
    candidates = [meta.get("code_count"), meta.get("total")]
    extra = meta.get("extra")
    if isinstance(extra, dict):
        candidates.append(extra.get("code_count"))
    for candidate in candidates:
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            return int(candidate)
    return None


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _component_page_info(component: Dict[str, Any]) -> Dict[str, Optional[int]]:
    data = component["data"]
    meta = data.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    rows = data.get("datas")
    return {
        "page": _optional_int(meta.get("page")),
        "limit": _optional_int(meta.get("limit")),
        "reported_total_count": _reported_total(meta),
        "returned_row_count": len(rows) if isinstance(rows, list) else 0,
    }


def _column_descriptors(
    component: Dict[str, Any],
    *,
    mappings: Dict[str, Dict[str, Any]],
    mapping_version: str,
) -> List[Dict[str, Any]]:
    descriptors: List[Dict[str, Any]] = []
    for column in component["data"]["columns"]:
        if not isinstance(column, dict):
            continue
        raw_field_name = column.get("key") or column.get("index_name")
        if not isinstance(raw_field_name, str) or not raw_field_name:
            continue
        parsed = parse_field_name(
            raw_field_name,
            mappings=mappings,
            mapping_version=mapping_version,
        )
        descriptors.append(
            {
                "row_key": raw_field_name,
                "parsed": parsed,
                "source_index_name": column.get("index_name"),
                "source_role": column.get("source"),
                "source_type": column.get("type"),
                "source_unit": column.get("unit") or None,
                "source_timestamp": column.get("timestamp") or None,
            }
        )
    return descriptors


def _select_market_component(
    payload: Dict[str, Any],
    *,
    mappings: Dict[str, Dict[str, Any]],
    mapping_version: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    required = {
        "security_code",
        "security_name",
        "market_memberships",
        "close",
        "market_cap",
        "pe_ttm",
    }
    candidates: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    for component in extract_table_components(payload):
        descriptors = _column_descriptors(
            component,
            mappings=mappings,
            mapping_version=mapping_version,
        )
        canonical_names = {
            descriptor["parsed"]["canonical_field_name"]
            for descriptor in descriptors
            if descriptor["parsed"]["mapping_status"] == "mapped"
        }
        if required.issubset(canonical_names):
            candidates.append((component, descriptors))

    if not candidates:
        raise NormalizationError("no table component contains all core market fields")
    if len(candidates) > 1:
        raise NormalizationError("multiple table components contain core market fields")
    return candidates[0]


def _matching_descriptors(
    descriptors: List[Dict[str, Any]],
    canonical_name: str,
    *,
    adjustment_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    matches = []
    for descriptor in descriptors:
        parsed = descriptor["parsed"]
        if parsed["canonical_field_name"] != canonical_name:
            continue
        if adjustment_type is not None and parsed["adjustment_type"] != adjustment_type:
            continue
        matches.append(descriptor)
    return matches


def _unique_descriptor(
    descriptors: List[Dict[str, Any]],
    canonical_name: str,
    *,
    adjustment_type: Optional[str] = None,
) -> Dict[str, Any]:
    matches = _matching_descriptors(
        descriptors,
        canonical_name,
        adjustment_type=adjustment_type,
    )
    if len(matches) != 1:
        qualifier = f" ({adjustment_type})" if adjustment_type else ""
        raise NormalizationError(
            f"expected exactly one {canonical_name}{qualifier} column; found {len(matches)}"
        )
    return matches[0]


def _optional_descriptor(
    descriptors: List[Dict[str, Any]],
    canonical_name: str,
    *,
    adjustment_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    matches = _matching_descriptors(
        descriptors,
        canonical_name,
        adjustment_type=adjustment_type,
    )
    if len(matches) > 1:
        qualifier = f" ({adjustment_type})" if adjustment_type else ""
        raise NormalizationError(
            f"expected at most one {canonical_name}{qualifier} column; found {len(matches)}"
        )
    return matches[0] if matches else None


def _row_value(
    row: Dict[str, Any],
    descriptor: Optional[Dict[str, Any]],
) -> Tuple[bool, Any]:
    if descriptor is None:
        return False, None
    row_key = descriptor["row_key"]
    if row_key not in row or row[row_key] in (None, ""):
        return False, None
    return True, row[row_key]


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    return _required_text(value, field_name)


def _required_number(value: Any, field_name: str) -> Any:
    if isinstance(value, bool):
        raise NormalizationError(f"{field_name} must be numeric")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NormalizationError(f"{field_name} must be finite")
        return value
    if isinstance(value, str) and value.strip():
        try:
            number = float(value)
        except ValueError as error:
            raise NormalizationError(f"{field_name} must be numeric") from error
        if not math.isfinite(number):
            raise NormalizationError(f"{field_name} must be finite")
        return number
    raise NormalizationError(f"{field_name} must be numeric")


def _optional_number(value: Any, field_name: str) -> Optional[Any]:
    if value in (None, ""):
        return None
    return _required_number(value, field_name)


def _optional_date(value: Any, field_name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not COMPACT_DATE_PATTERN.fullmatch(text):
        raise NormalizationError(f"{field_name} must use YYYYMMDD")
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError as error:
        raise NormalizationError(f"{field_name} contains an invalid date") from error


def _market_memberships(value: Any) -> List[str]:
    if isinstance(value, str):
        raw_memberships = value.split(";")
    elif isinstance(value, list):
        if any(not isinstance(item, str) for item in value):
            raise NormalizationError(
                "market_memberships list must contain only strings"
            )
        raw_memberships = value
    else:
        raise NormalizationError(
            "market_memberships must be a string or string list"
        )
    memberships: List[str] = []
    for item in raw_memberships:
        normalized = item.strip()
        if normalized and normalized not in memberships:
            memberships.append(normalized)
    if not memberships:
        raise NormalizationError("market_memberships must contain at least one value")
    return memberships


def _metadata_as_of_date(metadata: Dict[str, Any]) -> str:
    value = metadata.get("as_of_date")
    if not isinstance(value, str):
        raise NormalizationError(
            "context-free latest valuation fields require metadata.as_of_date"
        )
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as error:
        raise NormalizationError("metadata.as_of_date must use YYYY-MM-DD") from error


def _lineage(descriptor: Dict[str, Any], *, value_present: bool) -> Dict[str, Any]:
    parsed = descriptor["parsed"]
    return {
        "raw_field_name": parsed["raw_field_name"],
        "canonical_field_name": parsed["canonical_field_name"],
        "as_of_date": parsed["as_of_date"],
        "period_end": parsed["period_end"],
        "unit": parsed["unit"],
        "adjustment_type": parsed["adjustment_type"],
        "confidence": parsed["confidence"],
        "parser_version": parsed["parser_version"],
        "mapping_version": parsed["mapping_version"],
        "source_index_name": descriptor["source_index_name"],
        "source_role": descriptor["source_role"],
        "source_type": descriptor["source_type"],
        "source_unit": descriptor["source_unit"],
        "source_timestamp": descriptor["source_timestamp"],
        "value_status": "present" if value_present else "missing_in_source",
    }


def _field_lineage(
    selected: Dict[str, Optional[Dict[str, Any]]],
    names: Iterable[str],
    row: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for name in names:
        descriptor = selected.get(name)
        if descriptor is None:
            continue
        present, _ = _row_value(row, descriptor)
        result[name] = _lineage(descriptor, value_present=present)
    return result


def _record_context(
    metadata: Dict[str, Any],
    snapshot_path: Path,
    mapping_version: str,
    repository_root: Path,
) -> Dict[str, Any]:
    return {
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "source": metadata["source"],
        "fetched_at": metadata["fetched_at"],
        "raw_record_id": metadata["record_id"],
        "raw_snapshot": repository_relative_path(
            snapshot_path, repository_root=repository_root
        ),
        "normalizer_version": NORMALIZER_VERSION,
        "mapping_version": mapping_version,
    }


def _validate_unique_primary_keys(
    table_name: str,
    records: List[Dict[str, Any]],
) -> None:
    primary_key = PRIMARY_KEYS[table_name]
    seen = set()
    for record in records:
        key = tuple(record.get(field) for field in primary_key)
        if None in key:
            raise NormalizationError(f"{table_name} has an incomplete primary key: {key}")
        if key in seen:
            raise NormalizationError(f"{table_name} has a duplicate primary key: {key}")
        seen.add(key)


def _sort_and_validate_tables(tables: Dict[str, List[Dict[str, Any]]]) -> None:
    for table_name, records in tables.items():
        records.sort(
            key=lambda record: tuple(record[key] for key in PRIMARY_KEYS[table_name])
        )
        _validate_unique_primary_keys(table_name, records)


def _validate_price_bar(
    *,
    security_code: str,
    open_price: Optional[Any],
    high: Optional[Any],
    low: Optional[Any],
    close: Any,
    volume: Optional[Any],
    turnover: Optional[Any],
) -> None:
    prices = [price for price in (open_price, high, low, close) if price is not None]
    if any(price <= 0 for price in prices):
        raise NormalizationError(f"{security_code} contains a non-positive price")
    if high is not None and low is not None:
        if high < low:
            raise NormalizationError(f"{security_code} high is below low")
        for label, price in (("open", open_price), ("close", close)):
            if price is not None and not low <= price <= high:
                raise NormalizationError(
                    f"{security_code} {label} is outside the daily range"
                )
    if volume is not None and volume < 0:
        raise NormalizationError(f"{security_code} contains negative volume")
    if turnover is not None and turnover < 0:
        raise NormalizationError(f"{security_code} contains negative turnover")


def build_normalized_tables(
    snapshot_path: Path,
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    """Build deterministic normalized records for one raw snapshot."""
    metadata, payload = _load_envelope(snapshot_path)
    mapping_version, mappings = load_field_mappings(mapping_file)
    component, descriptors = _select_market_component(
        payload,
        mappings=mappings,
        mapping_version=mapping_version,
    )

    selected: Dict[str, Optional[Dict[str, Any]]] = {
        "security_code": _unique_descriptor(descriptors, "security_code"),
        "security_name": _unique_descriptor(descriptors, "security_name"),
        "market_memberships": _unique_descriptor(descriptors, "market_memberships"),
        "listing_date": _optional_descriptor(descriptors, "listing_date"),
        "listing_status": _optional_descriptor(descriptors, "listing_status"),
        "open": _optional_descriptor(
            descriptors,
            "open",
            adjustment_type="unadjusted",
        ),
        "high": _optional_descriptor(
            descriptors,
            "high",
            adjustment_type="unadjusted",
        ),
        "low": _optional_descriptor(
            descriptors,
            "low",
            adjustment_type="unadjusted",
        ),
        "close": _unique_descriptor(
            descriptors,
            "close",
            adjustment_type="unadjusted",
        ),
        "volume": _optional_descriptor(descriptors, "volume"),
        "turnover": _optional_descriptor(descriptors, "turnover"),
        "market_cap": _unique_descriptor(descriptors, "market_cap"),
        "pe_ttm": _unique_descriptor(descriptors, "pe_ttm"),
    }

    close_date = selected["close"]["parsed"]["as_of_date"]  # type: ignore[index]
    if not close_date:
        raise NormalizationError("close column must contain a valid trade date")
    for name in ("open", "high", "low", "volume", "turnover"):
        descriptor = selected[name]
        if descriptor is not None and descriptor["parsed"]["as_of_date"] != close_date:
            raise NormalizationError(f"{name} must share the close trade date")

    market_cap_date = selected["market_cap"]["parsed"]["as_of_date"]  # type: ignore[index]
    raw_pe_date = selected["pe_ttm"]["parsed"]["as_of_date"]  # type: ignore[index]
    if not market_cap_date:
        raise NormalizationError("market_cap must contain a valid as-of date")
    if raw_pe_date is None:
        pe_date = _metadata_as_of_date(metadata)
        pe_date_source = "metadata.as_of_date"
    else:
        pe_date = raw_pe_date
        pe_date_source = "field_lineage.pe_ttm.as_of_date"
    if market_cap_date != pe_date:
        raise NormalizationError("valuation fields must share one valid as-of date")

    fetched_at = _parse_timestamp(metadata["fetched_at"])
    observed_date = fetched_at.date().isoformat()
    context = _record_context(
        metadata,
        snapshot_path,
        mapping_version,
        repository_root,
    )
    tables: Dict[str, List[Dict[str, Any]]] = {
        table_name: [] for table_name in TABLE_FILES
    }

    rows = component["data"].get("datas")
    if not isinstance(rows, list) or not rows:
        raise NormalizationError("core market table must contain at least one row")

    skipped_market_bars = 0
    skipped_valuations = 0
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise NormalizationError(f"row {row_index} must be a JSON object")

        code_present, code_value = _row_value(row, selected["security_code"])
        if not code_present:
            raise NormalizationError(f"row {row_index} is missing security_code")
        security_code = _required_text(code_value, "security_code")
        if not SECURITY_CODE_PATTERN.fullmatch(security_code):
            raise NormalizationError(f"unsupported A-share security code: {security_code}")
        exchange = security_code.rsplit(".", 1)[1]

        name_present, name_value = _row_value(row, selected["security_name"])
        memberships_present, memberships_value = _row_value(
            row,
            selected["market_memberships"],
        )
        if not name_present or not memberships_present:
            raise NormalizationError(f"row {row_index} is missing security identity data")
        _, listing_date_value = _row_value(row, selected["listing_date"])
        _, listing_status_value = _row_value(row, selected["listing_status"])

        common = {**context, "security_code": security_code}
        tables["security_master"].append(
            {
                **common,
                "observed_date": observed_date,
                "exchange": exchange,
                "security_name": _required_text(name_value, "security_name"),
                "market_memberships": _market_memberships(memberships_value),
                "listing_date": _optional_date(listing_date_value, "listing_date"),
                "listing_status": _optional_text(
                    listing_status_value,
                    "listing_status",
                ),
                "field_lineage": _field_lineage(
                    selected,
                    (
                        "security_code",
                        "security_name",
                        "market_memberships",
                        "listing_date",
                        "listing_status",
                    ),
                    row,
                ),
                "derived_lineage": {
                    "exchange": {
                        "derived_from": "security_code",
                        "rule": "suffix_after_dot",
                        "normalizer_version": NORMALIZER_VERSION,
                    },
                    "market_memberships": {
                        "derived_from": "field_lineage.market_memberships",
                        "rule": (
                            "accept_string_or_string_list_trim_preserve_order_"
                            "deduplicate"
                        ),
                        "normalizer_version": NORMALIZER_VERSION,
                    }
                },
            }
        )

        close_present, close_value = _row_value(row, selected["close"])
        if close_present:
            _, open_value = _row_value(row, selected["open"])
            _, high_value = _row_value(row, selected["high"])
            _, low_value = _row_value(row, selected["low"])
            _, volume_value = _row_value(row, selected["volume"])
            _, turnover_value = _row_value(row, selected["turnover"])
            open_price = _optional_number(open_value, "open")
            high = _optional_number(high_value, "high")
            low = _optional_number(low_value, "low")
            close = _required_number(close_value, "close")
            volume = _optional_number(volume_value, "volume")
            turnover = _optional_number(turnover_value, "turnover")
            _validate_price_bar(
                security_code=security_code,
                open_price=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                turnover=turnover,
            )
            tables["market_bars_daily"].append(
                {
                    **common,
                    "trade_date": close_date,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "volume_unit": (
                        selected["volume"]["parsed"]["unit"]
                        if selected["volume"] is not None
                        else None
                    ),
                    "turnover": turnover,
                    "turnover_currency": (
                        selected["turnover"]["parsed"]["unit"]
                        if selected["turnover"] is not None
                        else None
                    ),
                    "currency": selected["close"]["parsed"]["unit"],  # type: ignore[index]
                    "adjustment_type": selected["close"]["parsed"][  # type: ignore[index]
                        "adjustment_type"
                    ],
                    "field_lineage": _field_lineage(
                        selected,
                        (
                            "security_code",
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                            "turnover",
                        ),
                        row,
                    ),
                }
            )
        else:
            skipped_market_bars += 1

        market_cap_present, market_cap_value = _row_value(row, selected["market_cap"])
        pe_present, pe_value = _row_value(row, selected["pe_ttm"])
        if pe_present and not market_cap_present:
            raise NormalizationError(
                f"{security_code} has incomplete valuation fields"
            )
        if market_cap_present:
            market_cap = _required_number(market_cap_value, "market_cap")
            if market_cap <= 0:
                raise NormalizationError(
                    f"{security_code} contains non-positive market_cap"
                )
            tables["valuation_snapshots"].append(
                {
                    **common,
                    "as_of_date": market_cap_date,
                    "market_cap": market_cap,
                    "market_cap_currency": selected["market_cap"]["parsed"][  # type: ignore[index]
                        "unit"
                    ],
                    "pe_ttm": _optional_number(pe_value, "pe_ttm"),
                    "field_lineage": _field_lineage(
                        selected,
                        ("security_code", "market_cap", "pe_ttm"),
                        row,
                    ),
                    "derived_lineage": {
                        "pe_ttm_as_of_date": {
                            "derived_from": pe_date_source,
                            "rule": (
                                "use_explicit_field_date_else_require_matching_"
                                "metadata_as_of_date"
                            ),
                            "value": pe_date,
                            "normalizer_version": NORMALIZER_VERSION,
                        }
                    },
                }
            )
        else:
            skipped_valuations += 1

    _sort_and_validate_tables(tables)
    unmapped_fields = sorted(
        descriptor["parsed"]["raw_field_name"]
        for descriptor in descriptors
        if descriptor["parsed"]["mapping_status"] == "unmapped"
    )
    return {
        "metadata": metadata,
        "snapshot_path": snapshot_path,
        "mapping_version": mapping_version,
        "tables": tables,
        "unmapped_fields": unmapped_fields,
        "page_info": _component_page_info(component),
        "skipped_market_bars": skipped_market_bars,
        "skipped_valuations": skipped_valuations,
    }


def _batch_record_id(
    raw_record_ids: Sequence[str],
    mapping_version: str,
) -> str:
    identity = "\0".join(
        [NORMALIZER_VERSION, mapping_version, *raw_record_ids]
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:20]


def build_normalized_batch(
    snapshot_paths: Sequence[Path],
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
    universe_path: Path = DEFAULT_UNIVERSE,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    """Combine a complete ordered page set into one normalized bundle."""
    if not snapshot_paths:
        raise NormalizationError("at least one raw snapshot is required")
    resolved_paths = [Path(path) for path in snapshot_paths]
    if len({path.resolve() for path in resolved_paths}) != len(resolved_paths):
        raise NormalizationError("raw snapshot paths must be unique")

    universe = load_investment_universe(universe_path)
    minimum_expected_count = universe["stocks"]["minimum_expected_count"]
    parts = [
        build_normalized_tables(
            path,
            mapping_file=mapping_file,
            repository_root=repository_root,
        )
        for path in resolved_paths
    ]
    if any(
        part["page_info"][field] is None
        for part in parts
        for field in ("page", "limit", "reported_total_count")
    ):
        raise NormalizationError(
            "market batch requires page, limit, and reported total metadata"
        )
    parts.sort(key=lambda part: part["page_info"]["page"])

    sources = {part["metadata"]["source"] for part in parts}
    queries = {part["metadata"].get("query") for part in parts}
    mapping_versions = {part["mapping_version"] for part in parts}
    if len(sources) != 1 or len(queries) != 1 or len(mapping_versions) != 1:
        raise NormalizationError("batch snapshots must share source, query, and mapping")
    mapping_version = parts[0]["mapping_version"]

    page_numbers = [part["page_info"]["page"] for part in parts]
    reported_limits = {part["page_info"]["limit"] for part in parts}
    reported_totals = {
        part["page_info"]["reported_total_count"]
        for part in parts
    }
    if len(reported_limits) != 1:
        raise NormalizationError("market batch pages must report one page limit")
    if len(reported_totals) != 1:
        raise NormalizationError("market batch pages must report one total row count")
    page_limit = next(iter(reported_limits))
    reported_total = next(iter(reported_totals))
    if page_limit is None or page_limit <= 0:
        raise NormalizationError("market batch page limit must be positive")
    if reported_total is None or reported_total <= 0:
        raise NormalizationError("market batch reported total must be positive")
    if reported_total < minimum_expected_count:
        raise NormalizationError(
            "market batch reported total "
            f"{reported_total} is below configured minimum {minimum_expected_count}"
        )
    expected_page_count = math.ceil(reported_total / page_limit)
    expected_pages = list(range(1, expected_page_count + 1))
    if page_numbers != expected_pages:
        raise NormalizationError(
            f"market batch pages must be complete and sequential: {page_numbers}"
        )

    tables: Dict[str, List[Dict[str, Any]]] = {
        table_name: [] for table_name in TABLE_FILES
    }
    for part in parts:
        for table_name in TABLE_FILES:
            tables[table_name].extend(part["tables"][table_name])
    _sort_and_validate_tables(tables)

    if len(tables["security_master"]) != reported_total:
        raise NormalizationError(
            "security_master count does not match the reported source total"
        )
    eligible_security_count = sum(
        stock_record_allowed(record, universe)
        for record in tables["security_master"]
    )
    if eligible_security_count < minimum_expected_count:
        raise NormalizationError(
            "market batch eligible security count "
            f"{eligible_security_count} is below configured minimum "
            f"{minimum_expected_count}"
        )

    timestamps = [
        _parse_timestamp(part["metadata"]["fetched_at"]) for part in parts
    ]
    raw_records = [
        {
            "record_id": part["metadata"]["record_id"],
            "payload_sha256": part["metadata"]["payload_sha256"],
            "snapshot": repository_relative_path(
                part["snapshot_path"], repository_root=repository_root
            ),
            "fetched_at": part["metadata"]["fetched_at"],
            **part["page_info"],
        }
        for part in parts
    ]
    raw_record_ids = [record["record_id"] for record in raw_records]
    bundle_id = (
        raw_record_ids[0]
        if len(raw_record_ids) == 1
        else _batch_record_id(raw_record_ids, mapping_version)
    )

    return {
        "metadata": {
            "source": parts[0]["metadata"]["source"],
            "query": parts[0]["metadata"].get("query"),
            "fetched_at_start": min(timestamps).isoformat(timespec="microseconds"),
            "fetched_at_end": max(timestamps).isoformat(timespec="microseconds"),
            "record_id": bundle_id,
            "raw_records": raw_records,
        },
        "mapping_version": mapping_version,
        "tables": tables,
        "unmapped_fields": sorted(
            {
                field
                for part in parts
                for field in part["unmapped_fields"]
            }
        ),
        "coverage": {
            "source_snapshot_count": len(parts),
            "page_count": len(parts),
            "expected_page_count": expected_page_count,
            "reported_total_count": reported_total,
            "security_master_count": len(tables["security_master"]),
            "eligible_security_count": eligible_security_count,
            "minimum_expected_count": minimum_expected_count,
            "universe_version": universe["universe_version"],
            "universe_id": universe["stocks"]["universe_id"],
            "market_bars_daily_count": len(tables["market_bars_daily"]),
            "valuation_snapshots_count": len(tables["valuation_snapshots"]),
            "skipped_market_bars": sum(
                part["skipped_market_bars"] for part in parts
            ),
            "skipped_valuations": sum(
                part["skipped_valuations"] for part in parts
            ),
        },
    }


def _jsonl_bytes(records: List[Dict[str, Any]]) -> bytes:
    lines = [
        json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for record in records
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_normalized_bundle(
    built: Dict[str, Any],
    *,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
) -> Path:
    """Atomically write one immutable single- or multi-snapshot bundle."""
    metadata = built["metadata"]
    fetched_at = _parse_timestamp(metadata["fetched_at_start"])
    destination = normalized_root.joinpath(
        "runs",
        metadata["source"],
        fetched_at.strftime("%Y"),
        fetched_at.strftime("%m"),
        fetched_at.strftime("%d"),
        metadata["record_id"],
    )
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite normalized bundle: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".normalizing-", dir=destination.parent))
    try:
        table_manifest: Dict[str, Dict[str, Any]] = {}
        for table_name, filename in TABLE_FILES.items():
            content = _jsonl_bytes(built["tables"][table_name])
            _write_bytes(staging / filename, content)
            table_manifest[table_name] = {
                "file": filename,
                "record_count": len(built["tables"][table_name]),
                "primary_key": PRIMARY_KEYS[table_name],
                "sha256": hashlib.sha256(content).hexdigest(),
            }

        manifest = {
            "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "mapping_version": built["mapping_version"],
            "bundle_id": metadata["record_id"],
            "source": metadata["source"],
            "query": metadata.get("query"),
            "fetched_at_start": metadata["fetched_at_start"],
            "fetched_at_end": metadata["fetched_at_end"],
            "raw_records": metadata["raw_records"],
            "coverage": built["coverage"],
            "tables": table_manifest,
            "unmapped_fields": built["unmapped_fields"],
        }
        manifest_content = (
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2)
            + "\n"
        ).encode("utf-8")
        _write_bytes(staging / "manifest.json", manifest_content)
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return destination


def normalize_snapshots(
    snapshot_paths: Sequence[Path],
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
    universe_path: Path = DEFAULT_UNIVERSE,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    built = build_normalized_batch(
        snapshot_paths,
        mapping_file=mapping_file,
        universe_path=universe_path,
        repository_root=repository_root,
    )
    return write_normalized_bundle(built, normalized_root=normalized_root)


def normalize_snapshot(
    snapshot_path: Path,
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
    universe_path: Path = DEFAULT_UNIVERSE,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    return normalize_snapshots(
        [snapshot_path],
        mapping_file=mapping_file,
        universe_path=universe_path,
        normalized_root=normalized_root,
        repository_root=repository_root,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize saved iWencai responses into core market tables."
    )
    parser.add_argument(
        "snapshots",
        nargs="+",
        type=Path,
        help="One or more saved raw-response snapshots",
    )
    parser.add_argument(
        "--mapping-file",
        type=Path,
        default=DEFAULT_MAPPING_FILE,
        help="Versioned field mapping JSON",
    )
    parser.add_argument(
        "--universe",
        type=Path,
        default=DEFAULT_UNIVERSE,
        help="Versioned investment-universe JSON",
    )
    parser.add_argument(
        "--normalized-root",
        type=Path,
        default=DEFAULT_NORMALIZED_ROOT,
        help="Normalized-data root",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        destination = normalize_snapshots(
            args.snapshots,
            mapping_file=args.mapping_file,
            universe_path=args.universe,
            normalized_root=args.normalized_root,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
