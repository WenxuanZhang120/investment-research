#!/usr/bin/env python3
"""Normalize saved iWencai ETF responses without entering the stock pipeline."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

from scripts.audit_iwencai_response import extract_table_components
from scripts.investment_universe import (
    DEFAULT_UNIVERSE,
    etf_index_family,
    load_investment_universe,
)
from scripts.parse_iwencai_fields import load_field_mappings, parse_field_name
from scripts.repository_paths import repository_relative_path


NORMALIZER_VERSION = "1.1.0"
BUNDLE_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1
DEFAULT_NORMALIZED_ROOT = REPOSITORY_ROOT / "data" / "normalized"
DEFAULT_ETF_MAPPING_FILE = REPOSITORY_ROOT / "config" / "etf_field_mappings.json"
PROJECT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
TABLE_FILE = "etf_snapshots.jsonl"
PRIMARY_KEY = ["etf_code", "as_of_date"]


class EtfNormalizationError(ValueError):
    """Raised when ETF data cannot be normalized without guessing."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise EtfNormalizationError("fetched_at must be an ISO 8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise EtfNormalizationError("fetched_at is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EtfNormalizationError("fetched_at must include a timezone")
    return parsed.astimezone(PROJECT_TIMEZONE)


def _load_envelope(path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EtfNormalizationError("raw snapshot root must be an object")
    metadata = value.get("metadata")
    payload = value.get("payload")
    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        raise EtfNormalizationError("raw snapshot must contain metadata and payload")
    if metadata.get("source") != "iwencai":
        raise EtfNormalizationError("ETF snapshot source must be iwencai")
    if hashlib.sha256(_canonical(payload)).hexdigest() != metadata.get("payload_sha256"):
        raise EtfNormalizationError("raw payload checksum does not match metadata")
    if not isinstance(metadata.get("record_id"), str) or not metadata["record_id"]:
        raise EtfNormalizationError("raw record_id is missing")
    _timestamp(metadata.get("fetched_at"))
    return metadata, payload


def _descriptors(
    component: Dict[str, Any], mappings: Dict[str, Dict[str, Any]], version: str
) -> List[Dict[str, Any]]:
    result = []
    for column in component["data"].get("columns", []):
        if not isinstance(column, dict):
            continue
        raw_name = column.get("key") or column.get("index_name")
        if not isinstance(raw_name, str) or not raw_name:
            continue
        result.append(
            {
                "row_key": raw_name,
                "parsed": parse_field_name(
                    raw_name, mappings=mappings, mapping_version=version
                ),
                "source_index_name": column.get("index_name"),
                "source_role": column.get("source"),
                "source_type": column.get("type"),
                "source_unit": column.get("unit") or None,
                "source_timestamp": column.get("timestamp") or None,
            }
        )
    return result


def _select_component(
    payload: Dict[str, Any], mappings: Dict[str, Dict[str, Any]], version: str
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    candidates = []
    for component in extract_table_components(payload):
        descriptors = _descriptors(component, mappings, version)
        names = {
            item["parsed"]["canonical_field_name"]
            for item in descriptors
            if item["parsed"]["mapping_status"] == "mapped"
        }
        if {"etf_code", "etf_name", "tracked_index"}.issubset(names):
            candidates.append((component, descriptors))
    if len(candidates) != 1:
        raise EtfNormalizationError(
            f"expected one ETF table component; found {len(candidates)}"
        )
    return candidates[0]


def _descriptor(
    descriptors: Iterable[Dict[str, Any]], canonical_name: str, *, required: bool
) -> Optional[Dict[str, Any]]:
    matches = [
        item
        for item in descriptors
        if item["parsed"]["canonical_field_name"] == canonical_name
    ]
    if len(matches) > 1 or (required and len(matches) != 1):
        raise EtfNormalizationError(
            f"expected {'one' if required else 'at most one'} {canonical_name}; "
            f"found {len(matches)}"
        )
    return matches[0] if matches else None


def _row_value(
    row: Dict[str, Any], descriptor: Optional[Dict[str, Any]]
) -> Tuple[bool, Any]:
    if descriptor is None:
        return False, None
    value = row.get(descriptor["row_key"])
    return (value not in (None, ""), value)


def _text(value: Any, label: str, *, required: bool = False) -> Optional[str]:
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EtfNormalizationError(f"{label} must be text")
    return value.strip()


def _string_memberships(value: Any, label: str) -> List[str]:
    if isinstance(value, str):
        raw_memberships = [value]
    elif isinstance(value, list):
        if any(not isinstance(item, str) for item in value):
            raise EtfNormalizationError(f"{label} list must contain only strings")
        raw_memberships = value
    else:
        raise EtfNormalizationError(f"{label} must be text or a text list")

    memberships: List[str] = []
    for item in raw_memberships:
        normalized = item.strip()
        if normalized and normalized not in memberships:
            memberships.append(normalized)
    if not memberships:
        raise EtfNormalizationError(f"{label} must contain at least one value")
    return memberships


def _canonical_fund_type(
    value: Any,
    required_type: str,
) -> Tuple[str, List[str]]:
    memberships = _string_memberships(value, "fund_type")
    required_upper = required_type.upper()
    if not any(required_upper in membership.upper() for membership in memberships):
        raise EtfNormalizationError(
            f"fund type is outside configured ETF scope: {memberships}"
        )
    return required_type, memberships


def _number(value: Any, label: str) -> Optional[Any]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise EtfNormalizationError(f"{label} must be numeric")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise EtfNormalizationError(f"{label} must be finite")
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError as error:
            raise EtfNormalizationError(f"{label} must be numeric") from error
        if math.isfinite(parsed):
            return parsed
    raise EtfNormalizationError(f"{label} must be finite numeric data")


def _date(value: Any, label: str) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError as error:
        raise EtfNormalizationError(f"{label} must use YYYYMMDD") from error


def _lineage(descriptor: Dict[str, Any], present: bool) -> Dict[str, Any]:
    parsed = descriptor["parsed"]
    return {
        "raw_field_name": parsed["raw_field_name"],
        "canonical_field_name": parsed["canonical_field_name"],
        "as_of_date": parsed["as_of_date"],
        "unit": parsed["unit"],
        "confidence": parsed["confidence"],
        "parser_version": parsed["parser_version"],
        "mapping_version": parsed["mapping_version"],
        "source_index_name": descriptor["source_index_name"],
        "source_role": descriptor["source_role"],
        "source_type": descriptor["source_type"],
        "source_unit": descriptor["source_unit"],
        "source_timestamp": descriptor["source_timestamp"],
        "value_status": "present" if present else "missing_in_source",
    }


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def build_etf_tables(
    snapshot_path: Path,
    *,
    mapping_file: Path = DEFAULT_ETF_MAPPING_FILE,
    universe_path: Path = DEFAULT_UNIVERSE,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    metadata, payload = _load_envelope(snapshot_path)
    mapping_version, mappings = load_field_mappings(mapping_file)
    universe = load_investment_universe(universe_path)
    component, descriptors = _select_component(payload, mappings, mapping_version)
    required = {
        name: _descriptor(descriptors, name, required=True)
        for name in ("etf_code", "etf_name", "tracked_index")
    }
    optional_names = (
        "etf_listing_date",
        "listing_status",
        "etf_price",
        "change_pct",
        "volume",
        "turnover",
        "fund_size",
        "nav",
        "premium_discount_rate",
        "management_fee_rate",
        "custody_fee_rate",
        "tracking_error",
    )
    selected = {
        **required,
        "fund_type": _descriptor(descriptors, "fund_type", required=True),
        **{
            name: _descriptor(descriptors, name, required=False)
            for name in optional_names
        },
    }
    rows = component["data"].get("datas")
    if not isinstance(rows, list) or not rows:
        raise EtfNormalizationError("ETF table must contain at least one row")
    fetched = _timestamp(metadata["fetched_at"])
    as_of_date = metadata.get("as_of_date") or fetched.date().isoformat()
    try:
        as_of_date = datetime.strptime(as_of_date, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError) as error:
        raise EtfNormalizationError("ETF as_of_date must use YYYY-MM-DD") from error
    records = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise EtfNormalizationError(f"ETF row {index} must be an object")
        values = {name: _row_value(row, descriptor)[1] for name, descriptor in selected.items()}
        code = _text(values["etf_code"], "etf_code", required=True)
        if code is None or not code.endswith(tuple(f".{x}" for x in universe["etfs"]["allowed_exchanges"])):
            raise EtfNormalizationError(f"ETF is outside allowed exchanges: {code}")
        tracked_index = _text(values["tracked_index"], "tracked_index", required=True)
        family = etf_index_family(tracked_index, universe)
        if family is None:
            raise EtfNormalizationError(f"ETF tracked index is outside configured scope: {tracked_index}")
        required_type = universe["etfs"]["required_fund_type"]
        fund_type, fund_type_memberships = _canonical_fund_type(
            values["fund_type"],
            required_type,
        )
        lineage = {
            name: _lineage(descriptor, _row_value(row, descriptor)[0])
            for name, descriptor in selected.items()
            if descriptor is not None
        }
        records.append(
            {
                "record_schema_version": RECORD_SCHEMA_VERSION,
                "source": metadata["source"],
                "fetched_at": metadata["fetched_at"],
                "raw_record_id": metadata["record_id"],
                "raw_snapshot": repository_relative_path(
                    snapshot_path, repository_root=repository_root
                ),
                "normalizer_version": NORMALIZER_VERSION,
                "mapping_version": mapping_version,
                "universe_version": universe["universe_version"],
                "universe_id": universe["etfs"]["universe_id"],
                "etf_code": code,
                "etf_name": _text(values["etf_name"], "etf_name", required=True),
                "exchange": code.rsplit(".", 1)[1],
                "as_of_date": as_of_date,
                "tracked_index": tracked_index,
                "tracked_index_family": family,
                "fund_type": fund_type,
                "fund_type_memberships": fund_type_memberships,
                "listing_date": _date(values["etf_listing_date"], "listing_date"),
                "listing_status": _text(values["listing_status"], "listing_status"),
                "price": _number(values["etf_price"], "price"),
                "change_pct": _number(values["change_pct"], "change_pct"),
                "volume": _number(values["volume"], "volume"),
                "turnover": _number(values["turnover"], "turnover"),
                "fund_size": _number(values["fund_size"], "fund_size"),
                "nav": _number(values["nav"], "nav"),
                "premium_discount_rate": _number(
                    values["premium_discount_rate"], "premium_discount_rate"
                ),
                "management_fee_rate": _number(
                    values["management_fee_rate"], "management_fee_rate"
                ),
                "custody_fee_rate": _number(
                    values["custody_fee_rate"], "custody_fee_rate"
                ),
                "tracking_error": _number(values["tracking_error"], "tracking_error"),
                "field_lineage": lineage,
                "derived_lineage": {
                    "exchange": {
                        "derived_from": "etf_code",
                        "rule": "suffix_after_dot",
                        "normalizer_version": NORMALIZER_VERSION,
                    },
                    "fund_type_memberships": {
                        "derived_from": "field_lineage.fund_type",
                        "rule": (
                            "accept_string_or_string_list_trim_preserve_order_"
                            "deduplicate"
                        ),
                        "normalizer_version": NORMALIZER_VERSION,
                    },
                    "fund_type": {
                        "derived_from": "fund_type_memberships",
                        "rule": "configured_required_fund_type_membership",
                        "required_fund_type": required_type,
                        "normalizer_version": NORMALIZER_VERSION,
                    },
                },
            }
        )
    records.sort(key=lambda item: (item["etf_code"], item["as_of_date"]))
    keys = [(item["etf_code"], item["as_of_date"]) for item in records]
    if len(keys) != len(set(keys)):
        raise EtfNormalizationError("ETF response contains duplicate code/date rows")
    meta = component["data"].get("meta")
    meta = meta if isinstance(meta, dict) else {}
    has_more = meta.get("has_more")
    if has_more is None and "has_more" in payload:
        has_more = payload.get("has_more")
    if has_more is not None and not isinstance(has_more, bool):
        raise EtfNormalizationError("ETF has_more must be boolean when present")
    return {
        "metadata": metadata,
        "snapshot_path": Path(snapshot_path),
        "mapping_version": mapping_version,
        "universe": universe,
        "records": records,
        "page": _optional_int(meta.get("page")),
        "limit": _optional_int(meta.get("limit")),
        "reported_total_count": _optional_int(meta.get("code_count") or meta.get("total")),
        "has_more": has_more,
        "unmapped_fields": sorted(
            item["parsed"]["raw_field_name"]
            for item in descriptors
            if item["parsed"]["mapping_status"] == "unmapped"
        ),
    }


def build_etf_batch(
    snapshot_paths: Sequence[Path],
    *,
    mapping_file: Path = DEFAULT_ETF_MAPPING_FILE,
    universe_path: Path = DEFAULT_UNIVERSE,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    if not snapshot_paths:
        raise EtfNormalizationError("at least one ETF snapshot is required")
    parts = [
        build_etf_tables(
            Path(path),
            mapping_file=mapping_file,
            universe_path=universe_path,
            repository_root=repository_root,
        )
        for path in snapshot_paths
    ]
    if len({part["metadata"].get("query") for part in parts}) != 1:
        raise EtfNormalizationError("ETF pages must share one query")
    if any(
        part[field] is None
        for part in parts
        for field in ("page", "limit", "reported_total_count")
    ):
        raise EtfNormalizationError(
            "ETF batch requires page, limit, and reported total metadata"
        )
    parts.sort(key=lambda item: item["page"])
    page_numbers = [part["page"] for part in parts]
    limits = {part["limit"] for part in parts}
    totals = {part["reported_total_count"] for part in parts}
    if len(limits) != 1:
        raise EtfNormalizationError("ETF pages report inconsistent limits")
    if len(totals) != 1:
        raise EtfNormalizationError("ETF pages report inconsistent totals")
    page_limit = next(iter(limits))
    total = next(iter(totals))
    if page_limit is None or page_limit <= 0:
        raise EtfNormalizationError("ETF page limit must be positive")
    if total is None or total <= 0:
        raise EtfNormalizationError("ETF reported total must be positive")
    expected_page_count = math.ceil(total / page_limit)
    if page_numbers != list(range(1, expected_page_count + 1)):
        raise EtfNormalizationError("ETF pages must be complete and sequential")
    for part in parts:
        expected_has_more = part["page"] < expected_page_count
        if part["has_more"] is not None and part["has_more"] is not expected_has_more:
            raise EtfNormalizationError(
                f"ETF page {part['page']} has_more conflicts with pagination"
            )
    records = [record for part in parts for record in part["records"]]
    records.sort(key=lambda item: (item["etf_code"], item["as_of_date"]))
    keys = [(item["etf_code"], item["as_of_date"]) for item in records]
    if len(keys) != len(set(keys)):
        raise EtfNormalizationError("ETF batch contains duplicate code/date rows")
    if len(records) != total:
        raise EtfNormalizationError(
            f"ETF batch expected {total} rows, found {len(records)}"
        )
    timestamps = [_timestamp(part["metadata"]["fetched_at"]) for part in parts]
    raw_ids = [part["metadata"]["record_id"] for part in parts]
    bundle_id = hashlib.sha256(
        "\0".join([NORMALIZER_VERSION, parts[0]["mapping_version"], *raw_ids]).encode()
    ).hexdigest()[:20]
    return {
        "bundle_id": bundle_id,
        "source": "iwencai",
        "query": parts[0]["metadata"].get("query"),
        "fetched_at_start": min(timestamps).isoformat(timespec="microseconds"),
        "fetched_at_end": max(timestamps).isoformat(timespec="microseconds"),
        "mapping_version": parts[0]["mapping_version"],
        "universe": parts[0]["universe"],
        "records": records,
        "raw_records": [
            {
                "record_id": part["metadata"]["record_id"],
                "snapshot": repository_relative_path(
                    part["snapshot_path"], repository_root=repository_root
                ),
                "payload_sha256": part["metadata"]["payload_sha256"],
                "page": part["page"],
                "limit": part["limit"],
                "reported_total_count": part["reported_total_count"],
                "has_more": part["has_more"],
            }
            for part in parts
        ],
        "unmapped_fields": sorted(
            {field for part in parts for field in part["unmapped_fields"]}
        ),
        "coverage": {
            "source_snapshot_count": len(parts),
            "page_count": len(parts),
            "expected_page_count": expected_page_count,
            "page_limit": page_limit,
            "reported_total_count": total,
            "etf_count": len(records),
            "tracked_index_family_counts": {
                family: sum(item["tracked_index_family"] == family for item in records)
                for family in parts[0]["universe"]["etfs"]["tracked_index_families"]
            },
        },
    }


def _jsonl(records: Sequence[Dict[str, Any]]) -> bytes:
    return (
        "\n".join(
            json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
            for item in records
        )
        + "\n"
    ).encode("utf-8")


def write_etf_bundle(
    built: Dict[str, Any], *, normalized_root: Path = DEFAULT_NORMALIZED_ROOT
) -> Path:
    fetched = _timestamp(built["fetched_at_start"])
    destination = Path(normalized_root).joinpath(
        "runs", "iwencai", fetched.strftime("%Y"), fetched.strftime("%m"),
        fetched.strftime("%d"), built["bundle_id"]
    )
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite ETF bundle: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".normalizing-etf-", dir=destination.parent))
    try:
        content = _jsonl(built["records"])
        with (staging / TABLE_FILE).open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        manifest = {
            "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
            "bundle_id": built["bundle_id"],
            "normalizer_version": NORMALIZER_VERSION,
            "mapping_version": built["mapping_version"],
            "source": built["source"],
            "query": built["query"],
            "fetched_at_start": built["fetched_at_start"],
            "fetched_at_end": built["fetched_at_end"],
            "universe_version": built["universe"]["universe_version"],
            "universe_id": built["universe"]["etfs"]["universe_id"],
            "raw_records": built["raw_records"],
            "coverage": built["coverage"],
            "tables": {
                "etf_snapshots": {
                    "file": TABLE_FILE,
                    "record_count": len(built["records"]),
                    "primary_key": PRIMARY_KEY,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            },
            "unmapped_fields": built["unmapped_fields"],
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return destination
