#!/usr/bin/env python3
"""Backfill connector-friendly small files for existing screening bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.screen_market_research_queue import (  # noqa: E402
    CONNECTOR_DIRECTORY,
    CONNECTOR_MAX_FILE_BYTES,
    ScreeningError,
    write_github_connector_export,
)


def _read_manifest(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ScreeningError(f"JSON root must be an object: {path}")
    return value


def _read_records(path: Path) -> List[Dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ScreeningError(f"row must be an object: {path}:{line_number}")
            records.append(record)
    return records


def export_screening_bundle(
    manifest_path: Path,
    *,
    max_file_bytes: int = CONNECTOR_MAX_FILE_BYTES,
) -> Path:
    manifest_path = Path(manifest_path)
    manifest = _read_manifest(manifest_path)
    table = manifest.get("table")
    if not isinstance(table, dict) or table.get("logical_name") != "market_research_queue":
        raise ScreeningError(f"not a screening queue manifest: {manifest_path}")
    source_path = manifest_path.parent / table["file"]
    content = source_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != table.get("sha256"):
        raise ScreeningError(f"source queue hash mismatch: {source_path}")
    records = _read_records(source_path)
    if len(records) != table.get("record_count"):
        raise ScreeningError(f"source queue record count mismatch: {source_path}")
    return write_github_connector_export(
        records,
        destination=manifest_path.parent / CONNECTOR_DIRECTORY,
        source_table=table,
        source_bundle_id=manifest["bundle_id"],
        max_file_bytes=max_file_bytes,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create <1 MiB GitHub-connector files for screening bundles."
    )
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument(
        "--max-file-bytes", type=int, default=CONNECTOR_MAX_FILE_BYTES
    )
    args = parser.parse_args(argv)
    try:
        for manifest in args.manifests:
            print(
                export_screening_bundle(
                    manifest,
                    max_file_bytes=args.max_file_bytes,
                )
            )
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
