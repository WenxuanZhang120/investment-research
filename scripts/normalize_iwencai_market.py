#!/usr/bin/env python3
"""Normalize one saved iWencai response into core market-data tables."""

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
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.audit_iwencai_response import extract_table_components  # noqa: E402
from scripts.parse_iwencai_fields import (  # noqa: E402
    DEFAULT_MAPPING_FILE,
    load_field_mappings,
    parse_field_name,
)


NORMALIZER_VERSION = "1.0.0"
RECORD_SCHEMA_VERSION = 1
DEFAULT_NORMALIZED_ROOT = REPOSITORY_ROOT / "data" / "normalized"
PROJECT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
SECURITY_CODE_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
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
    """Raised when a raw response cannot be normalized without guessing."""


def _canonical_payload(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


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
        "market_type",
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


def _unique_descriptor(
    descriptors: List[Dict[str, Any]],
    canonical_name: str,
    *,
    adjustment_type: Optional[str] = None,
) -> Dict[str, Any]:
    matches = []
    for descriptor in descriptors:
        parsed = descriptor["parsed"]
        if parsed["canonical_field_name"] != canonical_name:
            continue
        if adjustment_type is not None and parsed["adjustment_type"] != adjustment_type:
            continue
        matches.append(descriptor)
    if len(matches) != 1:
        qualifier = f" ({adjustment_type})" if adjustment_type else ""
        raise NormalizationError(
            f"expected exactly one {canonical_name}{qualifier} column; found {len(matches)}"
        )
    return matches[0]


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError(f"{field_name} must be a non-empty string")
    return value.strip()


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


def _lineage(descriptor: Dict[str, Any]) -> Dict[str, Any]:
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
    }


def _record_context(
    metadata: Dict[str, Any],
    snapshot_path: Path,
    mapping_version: str,
) -> Dict[str, Any]:
    return {
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "source": metadata["source"],
        "fetched_at": metadata["fetched_at"],
        "raw_record_id": metadata["record_id"],
        "raw_snapshot": _display_path(snapshot_path),
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


def build_normalized_tables(
    snapshot_path: Path,
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
) -> Dict[str, Any]:
    """Build deterministic normalized records without writing output files."""
    metadata, payload = _load_envelope(snapshot_path)
    mapping_version, mappings = load_field_mappings(mapping_file)
    component, descriptors = _select_market_component(
        payload,
        mappings=mappings,
        mapping_version=mapping_version,
    )

    selected = {
        "security_code": _unique_descriptor(descriptors, "security_code"),
        "security_name": _unique_descriptor(descriptors, "security_name"),
        "market_type": _unique_descriptor(descriptors, "market_type"),
        "close": _unique_descriptor(
            descriptors,
            "close",
            adjustment_type="unadjusted",
        ),
        "market_cap": _unique_descriptor(descriptors, "market_cap"),
        "pe_ttm": _unique_descriptor(descriptors, "pe_ttm"),
    }

    close_date = selected["close"]["parsed"]["as_of_date"]
    market_cap_date = selected["market_cap"]["parsed"]["as_of_date"]
    pe_date = selected["pe_ttm"]["parsed"]["as_of_date"]
    if not close_date:
        raise NormalizationError("close column must contain a valid trade date")
    if not market_cap_date or market_cap_date != pe_date:
        raise NormalizationError("valuation fields must share one valid as-of date")

    fetched_at = _parse_timestamp(metadata["fetched_at"])
    observed_date = fetched_at.date().isoformat()
    context = _record_context(metadata, snapshot_path, mapping_version)
    tables: Dict[str, List[Dict[str, Any]]] = {
        table_name: [] for table_name in TABLE_FILES
    }

    rows = component["data"].get("datas")
    if not isinstance(rows, list) or not rows:
        raise NormalizationError("core market table must contain at least one row")

    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise NormalizationError(f"row {row_index} must be a JSON object")

        def value(name: str) -> Any:
            row_key = selected[name]["row_key"]
            if row_key not in row:
                raise NormalizationError(f"row {row_index} is missing {row_key}")
            return row[row_key]

        security_code = _required_text(value("security_code"), "security_code")
        if not SECURITY_CODE_PATTERN.fullmatch(security_code):
            raise NormalizationError(f"unsupported A-share security code: {security_code}")

        common = {**context, "security_code": security_code}
        tables["security_master"].append(
            {
                **common,
                "observed_date": observed_date,
                "security_name": _required_text(value("security_name"), "security_name"),
                "market_type": _required_text(value("market_type"), "market_type"),
                "field_lineage": {
                    name: _lineage(selected[name])
                    for name in ("security_code", "security_name", "market_type")
                },
            }
        )
        tables["market_bars_daily"].append(
            {
                **common,
                "trade_date": close_date,
                "close": _required_number(value("close"), "close"),
                "currency": selected["close"]["parsed"]["unit"],
                "adjustment_type": selected["close"]["parsed"]["adjustment_type"],
                "field_lineage": {
                    name: _lineage(selected[name])
                    for name in ("security_code", "close")
                },
            }
        )
        tables["valuation_snapshots"].append(
            {
                **common,
                "as_of_date": market_cap_date,
                "market_cap": _required_number(value("market_cap"), "market_cap"),
                "market_cap_currency": selected["market_cap"]["parsed"]["unit"],
                "pe_ttm": _required_number(value("pe_ttm"), "pe_ttm"),
                "field_lineage": {
                    name: _lineage(selected[name])
                    for name in ("security_code", "market_cap", "pe_ttm")
                },
            }
        )

    for table_name, records in tables.items():
        records.sort(key=lambda record: tuple(record[key] for key in PRIMARY_KEYS[table_name]))
        _validate_unique_primary_keys(table_name, records)

    unmapped_fields = sorted(
        descriptor["parsed"]["raw_field_name"]
        for descriptor in descriptors
        if descriptor["parsed"]["mapping_status"] == "unmapped"
    )
    return {
        "metadata": metadata,
        "mapping_version": mapping_version,
        "tables": tables,
        "unmapped_fields": unmapped_fields,
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
    snapshot_path: Path,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
) -> Path:
    """Atomically write one immutable normalization bundle."""
    metadata = built["metadata"]
    fetched_at = _parse_timestamp(metadata["fetched_at"])
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
            "bundle_schema_version": 1,
            "normalizer_version": NORMALIZER_VERSION,
            "mapping_version": built["mapping_version"],
            "source": metadata["source"],
            "query": metadata.get("query"),
            "fetched_at": metadata["fetched_at"],
            "raw_record_id": metadata["record_id"],
            "raw_payload_sha256": metadata["payload_sha256"],
            "raw_snapshot": _display_path(snapshot_path),
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


def normalize_snapshot(
    snapshot_path: Path,
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
) -> Path:
    built = build_normalized_tables(snapshot_path, mapping_file=mapping_file)
    return write_normalized_bundle(
        built,
        snapshot_path=snapshot_path,
        normalized_root=normalized_root,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize one saved iWencai response into core market tables."
    )
    parser.add_argument("snapshot", type=Path, help="Saved raw-response snapshot")
    parser.add_argument(
        "--mapping-file",
        type=Path,
        default=DEFAULT_MAPPING_FILE,
        help="Versioned field mapping JSON",
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
        destination = normalize_snapshot(
            args.snapshot,
            mapping_file=args.mapping_file,
            normalized_root=args.normalized_root,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
