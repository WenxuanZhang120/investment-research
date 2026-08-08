#!/usr/bin/env python3
"""Normalize saved iWencai announcement search results into event records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAXONOMY = REPOSITORY_ROOT / "config" / "event_taxonomy.json"
DEFAULT_NORMALIZED_ROOT = REPOSITORY_ROOT / "data" / "normalized"
PROJECT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
NORMALIZER_VERSION = "1.1.0"


class AnnouncementNormalizationError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _load_snapshot(path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AnnouncementNormalizationError("snapshot root must be an object")
    metadata, payload = document.get("metadata"), document.get("payload")
    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        raise AnnouncementNormalizationError("snapshot envelope is incomplete")
    if metadata.get("source") != "iwencai":
        raise AnnouncementNormalizationError("snapshot source must be iwencai")
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    if digest != metadata.get("payload_sha256"):
        raise AnnouncementNormalizationError("raw payload checksum mismatch")
    if payload.get("status_code") != 0 or not isinstance(payload.get("data"), list):
        raise AnnouncementNormalizationError("snapshot is not a successful announcement response")
    return metadata, payload


def _timestamp(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 10_000_000_000 else value
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
    elif isinstance(value, str) and value:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise AnnouncementNormalizationError("invalid publish_time") from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=PROJECT_TIMEZONE)
    else:
        raise AnnouncementNormalizationError("publish_time is required")
    return parsed.astimezone(PROJECT_TIMEZONE).isoformat()


def _taxonomy(path: Path) -> Dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("rules"), list):
        raise AnnouncementNormalizationError("event taxonomy is invalid")
    return document


def _classify(title: str, taxonomy: Dict[str, Any]) -> Tuple[str, List[str]]:
    for rule in taxonomy["rules"]:
        matches = [keyword for keyword in rule["keywords"] if keyword in title]
        if matches:
            return rule["event_type"], matches
    return taxonomy["default_event_type"], []


def _source_security_identity(item: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    stock_infos = item.get("stock_infos")
    if not isinstance(stock_infos, list) or len(stock_infos) != 1:
        return None, None
    stock = stock_infos[0]
    if not isinstance(stock, dict):
        return None, None
    code = stock.get("code")
    name = stock.get("name")
    return (
        code if isinstance(code, str) and code else None,
        name if isinstance(name, str) and name else None,
    )


def build_events(snapshot_path: Path, *, taxonomy_path: Path = DEFAULT_TAXONOMY) -> Dict[str, Any]:
    snapshot_path = Path(snapshot_path)
    metadata, payload = _load_snapshot(snapshot_path)
    taxonomy = _taxonomy(taxonomy_path)
    records = []
    seen = set()
    for index, item in enumerate(payload["data"]):
        if not isinstance(item, dict):
            raise AnnouncementNormalizationError(f"data[{index}] must be an object")
        title, url = item.get("title"), item.get("url")
        if not isinstance(title, str) or not title.strip():
            raise AnnouncementNormalizationError(f"data[{index}] title is required")
        if not isinstance(url, str) or not url.strip():
            raise AnnouncementNormalizationError(f"data[{index}] url is required")
        published_at = _timestamp(item.get("publish_time"))
        event_type, keywords = _classify(title, taxonomy)
        source_security_code, security_name = _source_security_identity(item)
        identity = "\0".join(("iwencai", url, published_at, title)).encode("utf-8")
        event_id = hashlib.sha256(identity).hexdigest()[:24]
        if event_id in seen:
            continue
        seen.add(event_id)
        records.append(
            {
                "record_schema_version": 1,
                "event_id": event_id,
                "source": "iwencai",
                "title": title,
                "summary": item.get("summary") if isinstance(item.get("summary"), str) else None,
                "url": url,
                "published_at": published_at,
                "available_from": published_at,
                "event_type": event_type,
                "classification_keywords": keywords,
                "security_code": None,
                "source_security_code": source_security_code,
                "security_name": security_name,
                "query": metadata["query"],
                "fetched_at": metadata["fetched_at"],
                "raw_record_id": metadata["record_id"],
                "raw_snapshot": str(snapshot_path),
                "normalizer_version": NORMALIZER_VERSION,
                "taxonomy_version": taxonomy["taxonomy_version"],
                "raw_item": item,
            }
        )
    records.sort(key=lambda item: (item["published_at"], item["event_id"]))
    bundle_identity = "\0".join(
        (metadata["record_id"], NORMALIZER_VERSION, taxonomy["taxonomy_version"])
    ).encode("utf-8")
    return {
        "bundle_id": hashlib.sha256(bundle_identity).hexdigest()[:20],
        "records": records,
        "metadata": metadata,
        "taxonomy_version": taxonomy["taxonomy_version"],
    }


def write_bundle(built: Dict[str, Any], *, normalized_root: Path = DEFAULT_NORMALIZED_ROOT) -> Path:
    fetched = datetime.fromisoformat(built["metadata"]["fetched_at"])
    destination = normalized_root.joinpath(
        "runs", "iwencai", fetched.strftime("%Y"), fetched.strftime("%m"),
        fetched.strftime("%d"), built["bundle_id"],
    )
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite normalized bundle: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".normalizing-events-", dir=destination.parent))
    try:
        content = (
            "\n".join(json.dumps(x, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True) for x in built["records"])
            + ("\n" if built["records"] else "")
        ).encode("utf-8")
        with (staging / "events.jsonl").open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        manifest = {
            "bundle_schema_version": 1,
            "bundle_id": built["bundle_id"],
            "normalizer_version": NORMALIZER_VERSION,
            "taxonomy_version": built["taxonomy_version"],
            "source": "iwencai",
            "source_raw_record_id": built["metadata"]["record_id"],
            "fetched_at": built["metadata"]["fetched_at"],
            "coverage": {
                "record_count": len(built["records"]),
                "event_types": sorted({x["event_type"] for x in built["records"]}),
            },
            "table": {
                "logical_name": "events",
                "file": "events.jsonl",
                "primary_key": ["event_id"],
                "record_count": len(built["records"]),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return destination


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize an archived announcement response.")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--normalized-root", type=Path, default=DEFAULT_NORMALIZED_ROOT)
    args = parser.parse_args(argv)
    try:
        destination = write_bundle(
            build_events(args.snapshot, taxonomy_path=args.taxonomy),
            normalized_root=args.normalized_root,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
