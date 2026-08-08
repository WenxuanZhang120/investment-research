#!/usr/bin/env python3
"""Audit field coverage in a saved iWencai raw-response snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.parse_iwencai_fields import (  # noqa: E402
    DEFAULT_MAPPING_FILE,
    load_field_mappings,
    parse_field_name,
)


def extract_table_components(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Find table components in the observed iWencai response structure."""
    components: List[Dict[str, Any]] = []
    answers = payload.get("data", {}).get("answer", [])
    if not isinstance(answers, list):
        return components

    for answer in answers:
        if not isinstance(answer, dict):
            continue
        text_blocks = answer.get("txt", [])
        if not isinstance(text_blocks, list):
            continue
        for text_block in text_blocks:
            if not isinstance(text_block, dict):
                continue
            content = text_block.get("content")
            if not isinstance(content, dict):
                continue
            candidates = content.get("components", [])
            if not isinstance(candidates, list):
                continue
            for component in candidates:
                if not isinstance(component, dict):
                    continue
                data = component.get("data")
                if isinstance(data, dict) and isinstance(data.get("columns"), list):
                    components.append(component)

    return components


def _reported_total(meta: Dict[str, Any]) -> Optional[int]:
    direct_candidates = (meta.get("code_count"), meta.get("total"))
    for candidate in direct_candidates:
        if isinstance(candidate, int):
            return candidate

    extra = meta.get("extra")
    if isinstance(extra, dict) and isinstance(extra.get("code_count"), int):
        return extra["code_count"]
    return None


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def audit_snapshot(
    snapshot_path: Path,
    *,
    mapping_file: Path = DEFAULT_MAPPING_FILE,
) -> Dict[str, Any]:
    """Return a structured field coverage report for one saved snapshot."""
    envelope = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
        raise ValueError("snapshot must contain a JSON object payload")
    metadata = envelope.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("snapshot must contain metadata")

    mapping_version, mappings = load_field_mappings(mapping_file)
    components = extract_table_components(envelope["payload"])
    if not components:
        raise ValueError("no iWencai table components found in snapshot")

    audited_columns: List[Dict[str, Any]] = []
    returned_row_count = 0
    reported_totals: List[int] = []

    for component_index, component in enumerate(components):
        data = component["data"]
        rows = data.get("datas", [])
        if isinstance(rows, list):
            returned_row_count += len(rows)

        meta = data.get("meta", {})
        if isinstance(meta, dict):
            total = _reported_total(meta)
            if total is not None:
                reported_totals.append(total)

        for column in data["columns"]:
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
            audited_columns.append(
                {
                    **parsed,
                    "component_index": component_index,
                    "source_index_name": column.get("index_name"),
                    "source_role": column.get("source"),
                    "source_type": column.get("type"),
                    "source_unit": column.get("unit") or None,
                    "source_timestamp": column.get("timestamp") or None,
                }
            )

    mapped_count = sum(
        column["mapping_status"] == "mapped" for column in audited_columns
    )
    return {
        "raw_snapshot": _display_path(snapshot_path),
        "source": metadata.get("source"),
        "query": metadata.get("query"),
        "fetched_at": metadata.get("fetched_at"),
        "record_id": metadata.get("record_id"),
        "mapping_version": mapping_version,
        "summary": {
            "table_component_count": len(components),
            "column_count": len(audited_columns),
            "mapped_column_count": mapped_count,
            "unmapped_column_count": len(audited_columns) - mapped_count,
            "returned_row_count": returned_row_count,
            "reported_total_count": max(reported_totals) if reported_totals else None,
        },
        "columns": audited_columns,
    }


def write_report(report: Dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        raise FileExistsError(f"refusing to overwrite report: {output_path}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit field coverage in a saved iWencai raw snapshot."
    )
    parser.add_argument("snapshot", type=Path, help="Path to a saved raw snapshot")
    parser.add_argument(
        "--mapping-file",
        type=Path,
        default=DEFAULT_MAPPING_FILE,
        help="Path to the versioned JSON field mapping",
    )
    parser.add_argument("--output", type=Path, help="Optional exclusive-create report path")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit_snapshot(args.snapshot, mapping_file=args.mapping_file)
        if args.output:
            write_report(report, args.output)
            print(args.output)
        else:
            json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
