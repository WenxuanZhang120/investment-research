#!/usr/bin/env python3
"""Normalize saved iWencai responses into point-in-time financial facts."""

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
from scripts.parse_iwencai_fields import (  # noqa: E402
    DEFAULT_MAPPING_FILE,
    load_field_mappings,
    parse_field_name,
)


NORMALIZER_VERSION = "1.0.0"
RECORD_SCHEMA_VERSION = 1
BUNDLE_SCHEMA_VERSION = 1
DEFAULT_NORMALIZED_ROOT = REPOSITORY_ROOT / "data" / "normalized"
PROJECT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
SECURITY_CODE_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
COMPACT_DATE_PATTERN = re.compile(r"^\d{8}$")
REPORT_LABEL_PATTERN = re.compile(
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
TABLE_FILES = {
    "financial_reports": "financial_reports.jsonl",
    "financial_facts": "financial_facts.jsonl",
}
PRIMARY_KEYS = {
    "financial_reports": ["security_code", "period_end", "raw_record_id"],
    "financial_facts": [
        "security_code",
        "period_end",
        "canonical_field_name",
        "raw_record_id",
    ],
}


class FinancialNormalizationError(ValueError):
    """Raised when financial responses cannot be normalized without guessing."""


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
        raise FinancialNormalizationError(
            "raw metadata fetched_at must be an ISO 8601 string"
        )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise FinancialNormalizationError("raw metadata fetched_at is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FinancialNormalizationError(
            "raw metadata fetched_at must include a timezone"
        )
    return parsed.astimezone(PROJECT_TIMEZONE)


def _load_envelope(snapshot_path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    document = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise FinancialNormalizationError("raw snapshot root must be an object")
    metadata = document.get("metadata")
    payload = document.get("payload")
    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        raise FinancialNormalizationError(
            "raw snapshot must contain metadata and an object payload"
        )
    if metadata.get("source") != "iwencai":
        raise FinancialNormalizationError("raw snapshot source must be iwencai")

    expected_hash = metadata.get("payload_sha256")
    actual_hash = hashlib.sha256(_canonical_payload(payload)).hexdigest()
    if not isinstance(expected_hash, str) or expected_hash != actual_hash:
        raise FinancialNormalizationError(
            "raw payload checksum does not match metadata"
        )
    if not isinstance(metadata.get("record_id"), str) or not metadata["record_id"]:
        raise FinancialNormalizationError(
            "raw metadata record_id must be a non-empty string"
        )
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
        descriptors.append(
            {
                "row_key": raw_field_name,
                "parsed": parse_field_name(
                    raw_field_name,
                    mappings=mappings,
                    mapping_version=mapping_version,
                ),
                "source_index_name": column.get("index_name"),
                "source_role": column.get("source"),
                "source_type": column.get("type"),
                "source_unit": column.get("unit") or None,
                "source_timestamp": column.get("timestamp") or None,
            }
        )
    return descriptors


def _select_financial_component(
    payload: Dict[str, Any],
    *,
    mappings: Dict[str, Dict[str, Any]],
    mapping_version: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
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
        has_financial_fact = any(
            descriptor["parsed"]["category"] == "financial"
            for descriptor in descriptors
        )
        required = {
            "security_code",
            "security_name",
            "filing_date",
            "report_period_label",
        }
        if required.issubset(canonical_names) and has_financial_fact:
            candidates.append((component, descriptors))

    if not candidates:
        raise FinancialNormalizationError(
            "no table component contains financial facts and report metadata"
        )
    if len(candidates) > 1:
        raise FinancialNormalizationError(
            "multiple table components contain financial facts"
        )
    return candidates[0]


def _matching_descriptors(
    descriptors: Iterable[Dict[str, Any]],
    canonical_name: str,
    *,
    period_end: Optional[str] = None,
) -> List[Dict[str, Any]]:
    matches = []
    for descriptor in descriptors:
        parsed = descriptor["parsed"]
        if parsed["canonical_field_name"] != canonical_name:
            continue
        if period_end is not None and parsed["period_end"] != period_end:
            continue
        matches.append(descriptor)
    return matches


def _unique_descriptor(
    descriptors: Iterable[Dict[str, Any]],
    canonical_name: str,
    *,
    period_end: Optional[str] = None,
) -> Dict[str, Any]:
    matches = _matching_descriptors(
        descriptors,
        canonical_name,
        period_end=period_end,
    )
    if len(matches) != 1:
        qualifier = f" for {period_end}" if period_end else ""
        raise FinancialNormalizationError(
            f"expected exactly one {canonical_name}{qualifier}; found {len(matches)}"
        )
    return matches[0]


def _row_value(
    row: Dict[str, Any],
    descriptor: Dict[str, Any],
) -> Tuple[bool, Any]:
    key = descriptor["row_key"]
    if key not in row or row[key] in (None, ""):
        return False, None
    return True, row[key]


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinancialNormalizationError(
            f"{field_name} must be a non-empty string"
        )
    return value.strip()


def _optional_number(value: Any, field_name: str) -> Optional[Any]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise FinancialNormalizationError(f"{field_name} must be numeric")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FinancialNormalizationError(f"{field_name} must be finite")
        return value
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError as error:
            raise FinancialNormalizationError(
                f"{field_name} must be numeric"
            ) from error
        if not math.isfinite(number):
            raise FinancialNormalizationError(f"{field_name} must be finite")
        return number
    raise FinancialNormalizationError(f"{field_name} must be numeric")


def _parse_compact_date(value: Any, field_name: str) -> str:
    text = str(value).strip()
    if not COMPACT_DATE_PATTERN.fullmatch(text):
        raise FinancialNormalizationError(f"{field_name} must use YYYYMMDD")
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError as error:
        raise FinancialNormalizationError(
            f"{field_name} contains an invalid date"
        ) from error


def _parse_report_label(value: Any) -> Tuple[str, str, str]:
    label = _required_text(value, "report_period_label")
    match = REPORT_LABEL_PATTERN.fullmatch(label)
    if not match:
        raise FinancialNormalizationError(
            f"unsupported report period label: {label}"
        )
    month_day, suffix = REPORT_PERIODS[match.group("label")]
    year = match.group("year")
    return label, f"{year}-{month_day}", f"{year}{suffix}"


def _lineage(
    descriptor: Dict[str, Any],
    *,
    value_present: bool,
) -> Dict[str, Any]:
    parsed = descriptor["parsed"]
    return {
        "raw_field_name": parsed["raw_field_name"],
        "canonical_field_name": parsed["canonical_field_name"],
        "as_of_date": parsed["as_of_date"],
        "period_end": parsed["period_end"],
        "unit": parsed["unit"],
        "adjustment_type": parsed["adjustment_type"],
        "statement_type": parsed["statement_type"],
        "value_nature": parsed["value_nature"],
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
    selected: Dict[str, Dict[str, Any]],
    row: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    result = {}
    for name, descriptor in selected.items():
        present, _ = _row_value(row, descriptor)
        result[name] = _lineage(descriptor, value_present=present)
    return result


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


def _sort_and_validate_tables(tables: Dict[str, List[Dict[str, Any]]]) -> None:
    for table_name, records in tables.items():
        key_fields = PRIMARY_KEYS[table_name]
        records.sort(key=lambda record: tuple(record[field] for field in key_fields))
        seen = set()
        for record in records:
            key = tuple(record.get(field) for field in key_fields)
            if None in key:
                raise FinancialNormalizationError(
                    f"{table_name} has an incomplete primary key: {key}"
                )
            if key in seen:
                raise FinancialNormalizationError(
                    f"{table_name} has a duplicate primary key: {key}"
                )
            seen.add(key)


def build_financial_tables(
    snapshot_path: Path,
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
) -> Dict[str, Any]:
    """Build financial report metadata and long-form facts for one snapshot."""
    metadata, payload = _load_envelope(snapshot_path)
    mapping_version, mappings = load_field_mappings(mapping_file)
    component, descriptors = _select_financial_component(
        payload,
        mappings=mappings,
        mapping_version=mapping_version,
    )
    code_descriptor = _unique_descriptor(descriptors, "security_code")
    name_descriptor = _unique_descriptor(descriptors, "security_name")

    financial_descriptors = [
        descriptor
        for descriptor in descriptors
        if descriptor["parsed"]["category"] == "financial"
    ]
    period_ends = sorted(
        {
            descriptor["parsed"]["period_end"]
            for descriptor in financial_descriptors
            if descriptor["parsed"]["period_end"]
        }
    )
    if not period_ends:
        raise FinancialNormalizationError(
            "financial fields must contain at least one report period"
        )

    period_groups = {}
    for period_end in period_ends:
        period_facts = [
            descriptor
            for descriptor in financial_descriptors
            if descriptor["parsed"]["period_end"] == period_end
        ]
        canonical_names = [
            descriptor["parsed"]["canonical_field_name"]
            for descriptor in period_facts
        ]
        if len(canonical_names) != len(set(canonical_names)):
            raise FinancialNormalizationError(
                f"duplicate canonical financial fields for {period_end}"
            )
        period_groups[period_end] = {
            "facts": period_facts,
            "filing_date": _unique_descriptor(
                descriptors,
                "filing_date",
                period_end=period_end,
            ),
            "report_period_label": _unique_descriptor(
                descriptors,
                "report_period_label",
                period_end=period_end,
            ),
        }

    rows = component["data"].get("datas")
    if not isinstance(rows, list) or not rows:
        raise FinancialNormalizationError(
            "financial table must contain at least one row"
        )
    reported_total = payload.get("code_count")
    if isinstance(reported_total, int) and reported_total != len(rows):
        raise FinancialNormalizationError(
            "financial snapshot is incomplete; returned rows do not match code_count"
        )
    if payload.get("has_more") is True:
        raise FinancialNormalizationError(
            "financial snapshot has more pages and cannot be normalized alone"
        )

    fetched_at = _parse_timestamp(metadata["fetched_at"])
    common_context = _record_context(metadata, snapshot_path, mapping_version)
    tables = {table_name: [] for table_name in TABLE_FILES}

    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise FinancialNormalizationError(f"row {row_index} must be an object")
        code_present, code_value = _row_value(row, code_descriptor)
        name_present, name_value = _row_value(row, name_descriptor)
        if not code_present or not name_present:
            raise FinancialNormalizationError(
                f"row {row_index} is missing security identity"
            )
        security_code = _required_text(code_value, "security_code")
        if not SECURITY_CODE_PATTERN.fullmatch(security_code):
            raise FinancialNormalizationError(
                f"unsupported A-share security code: {security_code}"
            )
        security_name = _required_text(name_value, "security_name")

        for period_end, period_group in period_groups.items():
            filing_present, filing_value = _row_value(
                row,
                period_group["filing_date"],
            )
            label_present, label_value = _row_value(
                row,
                period_group["report_period_label"],
            )
            if not filing_present or not label_present:
                raise FinancialNormalizationError(
                    f"{security_code} {period_end} is missing report metadata"
                )
            filing_date = _parse_compact_date(filing_value, "filing_date")
            report_label, label_period_end, report_type = _parse_report_label(
                label_value
            )
            descriptor_report_type = period_group["filing_date"]["parsed"][
                "report_type"
            ]
            if label_period_end != period_end or descriptor_report_type != report_type:
                raise FinancialNormalizationError(
                    f"{security_code} report label does not match field period {period_end}"
                )
            if filing_date < period_end:
                raise FinancialNormalizationError(
                    f"{security_code} filing date precedes period end"
                )
            if filing_date > fetched_at.date().isoformat():
                raise FinancialNormalizationError(
                    f"{security_code} filing date is later than fetched_at"
                )

            context = {
                **common_context,
                "security_code": security_code,
                "security_name": security_name,
                "period_end": period_end,
                "report_type": report_type,
                "report_period_label": report_label,
                "filing_date": filing_date,
                "available_from": filing_date,
            }
            present_fact_count = 0
            for descriptor in period_group["facts"]:
                parsed = descriptor["parsed"]
                value_present, raw_value = _row_value(row, descriptor)
                value = _optional_number(
                    raw_value,
                    parsed["canonical_field_name"],
                )
                if value_present:
                    present_fact_count += 1
                canonical_name = parsed["canonical_field_name"]
                tables["financial_facts"].append(
                    {
                        **context,
                        "canonical_field_name": canonical_name,
                        "statement_type": parsed["statement_type"],
                        "value_nature": parsed["value_nature"],
                        "value": value,
                        "unit": parsed["unit"],
                        "value_status": (
                            "present" if value_present else "missing_in_source"
                        ),
                        "field_lineage": {
                            "security_code": _lineage(
                                code_descriptor,
                                value_present=True,
                            ),
                            "security_name": _lineage(
                                name_descriptor,
                                value_present=True,
                            ),
                            canonical_name: _lineage(
                                descriptor,
                                value_present=value_present,
                            ),
                        },
                    }
                )

            tables["financial_reports"].append(
                {
                    **context,
                    "fact_count": len(period_group["facts"]),
                    "present_fact_count": present_fact_count,
                    "missing_fact_count": (
                        len(period_group["facts"]) - present_fact_count
                    ),
                    "field_lineage": _field_lineage(
                        {
                            "security_code": code_descriptor,
                            "security_name": name_descriptor,
                            "filing_date": period_group["filing_date"],
                            "report_period_label": period_group[
                                "report_period_label"
                            ],
                        },
                        row,
                    ),
                }
            )

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
        "period_ends": period_ends,
        "reported_total_count": reported_total,
        "returned_row_count": len(rows),
    }


def _bundle_id(raw_record_ids: Sequence[str], mapping_version: str) -> str:
    identity = "\0".join(
        [NORMALIZER_VERSION, mapping_version, *raw_record_ids]
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:20]


def build_financial_batch(
    snapshot_paths: Sequence[Path],
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
) -> Dict[str, Any]:
    """Combine independent financial-period snapshots into one bundle."""
    if not snapshot_paths:
        raise FinancialNormalizationError("at least one snapshot is required")
    paths = [Path(path) for path in snapshot_paths]
    if len({path.resolve() for path in paths}) != len(paths):
        raise FinancialNormalizationError("snapshot paths must be unique")

    parts = [
        build_financial_tables(path, mapping_file=mapping_file) for path in paths
    ]
    parts.sort(key=lambda part: _parse_timestamp(part["metadata"]["fetched_at"]))
    if len({part["metadata"]["source"] for part in parts}) != 1:
        raise FinancialNormalizationError("batch snapshots must share one source")
    if len({part["mapping_version"] for part in parts}) != 1:
        raise FinancialNormalizationError(
            "batch snapshots must share one mapping version"
        )

    tables = {table_name: [] for table_name in TABLE_FILES}
    for part in parts:
        for table_name in TABLE_FILES:
            tables[table_name].extend(part["tables"][table_name])
    _sort_and_validate_tables(tables)

    timestamps = [
        _parse_timestamp(part["metadata"]["fetched_at"]) for part in parts
    ]
    mapping_version = parts[0]["mapping_version"]
    raw_records = [
        {
            "record_id": part["metadata"]["record_id"],
            "payload_sha256": part["metadata"]["payload_sha256"],
            "snapshot": _display_path(part["snapshot_path"]),
            "query": part["metadata"].get("query"),
            "fetched_at": part["metadata"]["fetched_at"],
            "period_ends": part["period_ends"],
            "reported_total_count": part["reported_total_count"],
            "returned_row_count": part["returned_row_count"],
        }
        for part in parts
    ]
    raw_record_ids = [record["record_id"] for record in raw_records]
    reports = tables["financial_reports"]
    facts = tables["financial_facts"]
    statement_counts = {
        statement_type: sum(
            fact["statement_type"] == statement_type for fact in facts
        )
        for statement_type in (
            "income_statement",
            "balance_sheet",
            "cash_flow_statement",
        )
    }

    return {
        "metadata": {
            "source": parts[0]["metadata"]["source"],
            "queries": [record["query"] for record in raw_records],
            "fetched_at_start": min(timestamps).isoformat(timespec="microseconds"),
            "fetched_at_end": max(timestamps).isoformat(timespec="microseconds"),
            "record_id": _bundle_id(raw_record_ids, mapping_version),
            "raw_records": raw_records,
        },
        "mapping_version": mapping_version,
        "tables": tables,
        "unmapped_fields": sorted(
            {field for part in parts for field in part["unmapped_fields"]}
        ),
        "coverage": {
            "source_snapshot_count": len(parts),
            "security_count": len({record["security_code"] for record in reports}),
            "period_ends": sorted({record["period_end"] for record in reports}),
            "report_types": sorted({record["report_type"] for record in reports}),
            "financial_report_count": len(reports),
            "financial_fact_count": len(facts),
            "present_fact_count": sum(
                fact["value_status"] == "present" for fact in facts
            ),
            "missing_fact_count": sum(
                fact["value_status"] == "missing_in_source" for fact in facts
            ),
            "fact_count_by_statement": statement_counts,
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


def write_financial_bundle(
    built: Dict[str, Any],
    *,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
) -> Path:
    """Atomically write an immutable financial bundle."""
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
        raise FileExistsError(
            f"refusing to overwrite normalized bundle: {destination}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".normalizing-", dir=destination.parent))
    try:
        table_manifest = {}
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
            "queries": metadata["queries"],
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


def normalize_financial_snapshots(
    snapshot_paths: Sequence[Path],
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
) -> Path:
    built = build_financial_batch(snapshot_paths, mapping_file=mapping_file)
    return write_financial_bundle(built, normalized_root=normalized_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize saved iWencai financial responses into long-form facts."
    )
    parser.add_argument(
        "snapshots",
        nargs="+",
        type=Path,
        help="One or more saved financial-response snapshots",
    )
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
        destination = normalize_financial_snapshots(
            args.snapshots,
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
