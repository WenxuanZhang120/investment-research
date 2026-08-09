#!/usr/bin/env python3
"""Save JSON responses as immutable, auditable raw-data snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence, Union


SCHEMA_VERSION = 1
PROJECT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_RAW_ROOT = Path(__file__).resolve().parents[1] / "data" / "raw"
SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def parse_fetched_at(value: Optional[str]) -> datetime:
    """Parse an ISO 8601 timestamp and normalize it to the project timezone."""
    if value is None:
        return datetime.now(PROJECT_TIMEZONE)

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone offset")
    return parsed.astimezone(PROJECT_TIMEZONE)


def load_json_payload(input_path: Path) -> Any:
    """Load a UTF-8 JSON document without applying domain transformations."""
    return json.loads(input_path.read_text(encoding="utf-8"))


def _canonical_payload(payload: Any) -> bytes:
    """Return deterministic JSON bytes used only for hashing and identity."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _append_query_log(log_path: Path, entry: dict[str, Any]) -> None:
    """Append one complete JSON line while holding an exclusive file lock."""
    import fcntl

    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n"

    with log_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def save_raw_response(
    payload: Any,
    *,
    source: str,
    query: str,
    raw_root: Union[str, Path] = DEFAULT_RAW_ROOT,
    fetched_at: Optional[datetime] = None,
    as_of_date: Optional[str] = None,
    raw_field_names: Optional[Sequence[str]] = None,
    collection_method: Optional[str] = None,
    collector_name: Optional[str] = None,
) -> Path:
    """Save a raw response envelope and return its path.

    The destination is opened in exclusive-create mode. An existing snapshot is
    never replaced, even when the same inputs and timestamp are supplied again.
    """
    normalized_source = source.strip().lower()
    if not SOURCE_PATTERN.fullmatch(normalized_source):
        raise ValueError(
            "source must contain only lowercase letters, digits, dots, underscores, or hyphens"
        )
    if normalized_source == "_query_log":
        raise ValueError("source name is reserved")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")

    normalized_fields: Optional[List[str]] = None
    if raw_field_names is not None:
        if (
            isinstance(raw_field_names, (str, bytes))
            or any(not isinstance(name, str) or not name for name in raw_field_names)
        ):
            raise ValueError("raw_field_names must contain non-empty strings")
        normalized_fields = sorted(set(raw_field_names))
        if len(normalized_fields) != len(raw_field_names):
            raise ValueError("raw_field_names must not contain duplicates")
    for label, value in (
        ("as_of_date", as_of_date),
        ("collection_method", collection_method),
        ("collector_name", collector_name),
    ):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{label} must be a non-empty string when supplied")

    timestamp = fetched_at or datetime.now(PROJECT_TIMEZONE)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    timestamp = timestamp.astimezone(PROJECT_TIMEZONE)

    payload_bytes = _canonical_payload(payload)
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    fetched_at_text = timestamp.isoformat(timespec="microseconds")
    identity = "\0".join(
        (normalized_source, query, fetched_at_text, payload_sha256)
    ).encode("utf-8")
    record_id = hashlib.sha256(identity).hexdigest()[:20]

    metadata = {
        "source": normalized_source,
        "query": query,
        "fetched_at": fetched_at_text,
        "record_id": record_id,
        "schema_version": SCHEMA_VERSION,
        "payload_sha256": payload_sha256,
    }
    if as_of_date is not None:
        metadata["as_of_date"] = as_of_date
    if normalized_fields is not None:
        metadata["raw_field_names"] = normalized_fields
    if collection_method is not None:
        metadata["collection_method"] = collection_method
    if collector_name is not None:
        metadata["collector_name"] = collector_name
    envelope = {"metadata": metadata, "payload": payload}

    root = Path(raw_root)
    date_parts = (
        timestamp.strftime("%Y"),
        timestamp.strftime("%m"),
        timestamp.strftime("%d"),
    )
    destination_dir = root.joinpath(normalized_source, *date_parts)
    destination_dir.mkdir(parents=True, exist_ok=True)
    filename_timestamp = timestamp.strftime("%Y%m%dT%H%M%S%f%z")
    destination = destination_dir / f"{filename_timestamp}_{record_id}.json"

    try:
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise FileExistsError(
            f"refusing to overwrite existing raw snapshot: {destination}"
        ) from None

    relative_path = destination.relative_to(root).as_posix()
    log_path = root.joinpath(
        "_query_log",
        timestamp.strftime("%Y"),
        timestamp.strftime("%m"),
        f"{timestamp:%d}.jsonl",
    )
    _append_query_log(
        log_path,
        {
            **metadata,
            "raw_relative_path": relative_path,
        },
    )

    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Save a local JSON response as an immutable raw-data snapshot."
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Stable source identifier, such as iwencai",
    )
    parser.add_argument("--query", required=True, help="Original query text")
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to a UTF-8 JSON response",
    )
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=DEFAULT_RAW_ROOT,
        help="Raw-data root directory (defaults to data/raw in this repository)",
    )
    parser.add_argument(
        "--fetched-at",
        help="Optional ISO 8601 fetch timestamp with timezone; defaults to the current time",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = load_json_payload(args.input)
        destination = save_raw_response(
            payload,
            source=args.source,
            query=args.query,
            raw_root=args.raw_root,
            fetched_at=parse_fetched_at(args.fetched_at),
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
