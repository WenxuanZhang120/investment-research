#!/usr/bin/env python3
"""Validate repository data lineage, manifests, query logs, and file limits."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GITHUB_FILE_LIMIT = 100 * 1024 * 1024


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_snapshots(root: Path) -> Iterable[Path]:
    raw_root = root / "data" / "raw"
    for source in raw_root.iterdir():
        if source.is_dir() and source.name != "_query_log":
            yield from source.glob("*/*/*/*.json")


def _validate_raw(root: Path, errors: List[str]) -> Dict[str, Dict[str, Any]]:
    metadata_by_relative = {}
    raw_root = root / "data" / "raw"
    for path in _raw_snapshots(root):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            metadata, payload = document["metadata"], document["payload"]
            digest = hashlib.sha256(_canonical(payload)).hexdigest()
            if digest != metadata.get("payload_sha256"):
                errors.append(f"{path}: payload hash mismatch")
            relative = path.relative_to(raw_root).as_posix()
            metadata_by_relative[relative] = metadata
        except (OSError, KeyError, TypeError, ValueError) as error:
            errors.append(f"{path}: invalid raw envelope ({error})")
    return metadata_by_relative


def _validate_query_logs(
    root: Path,
    raw_metadata: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> None:
    log_root = root / "data" / "raw" / "_query_log"
    logged = set()
    for path in log_root.glob("*/*/*.jsonl"):
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    entry = json.loads(line)
                    relative = entry["raw_relative_path"]
                except (json.JSONDecodeError, KeyError, TypeError) as error:
                    errors.append(f"{path}:{line_number}: invalid query-log entry ({error})")
                    continue
                if relative in logged:
                    errors.append(f"{path}:{line_number}: duplicate raw_relative_path")
                logged.add(relative)
                metadata = raw_metadata.get(relative)
                if metadata is None:
                    errors.append(f"{path}:{line_number}: raw snapshot does not exist")
                elif entry.get("record_id") != metadata.get("record_id"):
                    errors.append(f"{path}:{line_number}: record_id mismatch")
    missing = sorted(set(raw_metadata) - logged)
    for relative in missing:
        errors.append(f"{relative}: missing query-log entry")


def _table_files(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    tables = manifest.get("tables")
    if isinstance(tables, dict):
        for table in tables.values():
            if not isinstance(table, dict):
                continue
            partitions = table.get("partitions")
            if isinstance(partitions, list):
                entries.extend(partitions)
            elif isinstance(table.get("file"), str):
                entries.append(table)
    table = manifest.get("table")
    if isinstance(table, dict):
        partitions = table.get("partitions")
        if isinstance(partitions, list):
            entries.extend(partitions)
        elif isinstance(table.get("file"), str):
            entries.append(table)
    return entries


def _validate_manifests(root: Path, errors: List[str]) -> None:
    for area in ("normalized", "derived"):
        for path in (root / "data" / area).glob("runs/**/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                entries = _table_files(manifest)
                if not entries:
                    errors.append(f"{path}: manifest has no physical table files")
                for entry in entries:
                    target = path.parent / entry["file"]
                    if not target.is_file():
                        errors.append(f"{path}: missing table file {entry['file']}")
                    elif _sha(target) != entry.get("sha256"):
                        errors.append(f"{path}: hash mismatch for {entry['file']}")
            except (OSError, KeyError, TypeError, ValueError) as error:
                errors.append(f"{path}: invalid manifest ({error})")


def _validate_json_documents(root: Path, errors: List[str]) -> None:
    paths = list((root / "config").glob("*.json"))
    paths.extend((root / "portfolio").glob("*.template.json"))
    paths.extend((root / "research_queue").glob("*.template.json"))
    paths.extend((root / "decision_journal").glob("*.template.json"))
    for path in paths:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"{path}: invalid JSON ({error})")


def _validate_file_sizes(root: Path, errors: List[str]) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "private" in path.parts:
            continue
        if path.stat().st_size >= GITHUB_FILE_LIMIT:
            errors.append(f"{path}: exceeds GitHub 100 MiB file limit")


def validate_repository(root: Path = REPOSITORY_ROOT) -> List[str]:
    root = Path(root)
    errors: List[str] = []
    raw_metadata = _validate_raw(root, errors)
    _validate_query_logs(root, raw_metadata, errors)
    _validate_manifests(root, errors)
    _validate_json_documents(root, errors)
    _validate_file_sizes(root, errors)
    return errors


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate repository integrity.")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    errors = validate_repository(args.root)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("repository integrity validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
