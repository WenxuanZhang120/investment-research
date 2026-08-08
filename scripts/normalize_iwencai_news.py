#!/usr/bin/env python3
"""Normalize archived iWencai news results with point-in-time lineage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.normalize_iwencai_announcements import (  # noqa: E402
    DEFAULT_NORMALIZED_ROOT,
    DEFAULT_TAXONOMY,
    _classify,
    _load_snapshot,
    _source_security_identity,
    _taxonomy,
    _timestamp,
)


NORMALIZER_VERSION = "1.1.0"


def _publisher(item: Dict[str, Any]) -> Optional[str]:
    extra = item.get("extra")
    if isinstance(extra, dict):
        for field in ("real_publish_source", "publish_source"):
            value = extra.get(field)
            if isinstance(value, str) and value:
                return value
    return None


def build_news(
    snapshot_path: Path,
    *,
    taxonomy_path: Path = DEFAULT_TAXONOMY,
) -> Dict[str, Any]:
    snapshot_path = Path(snapshot_path)
    metadata, payload = _load_snapshot(snapshot_path)
    taxonomy = _taxonomy(taxonomy_path)
    records = []
    seen = set()
    for index, item in enumerate(payload["data"]):
        if not isinstance(item, dict):
            raise ValueError(f"data[{index}] must be an object")
        title, url = item.get("title"), item.get("url")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"data[{index}] title is required")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"data[{index}] url is required")
        published_at = _timestamp(item.get("publish_time"))
        event_type, keywords = _classify(title, taxonomy)
        source_security_code, security_name = _source_security_identity(item)
        identity = "\0".join(("iwencai-news", url, published_at, title)).encode()
        news_id = hashlib.sha256(identity).hexdigest()[:24]
        if news_id in seen:
            continue
        seen.add(news_id)
        publisher = _publisher(item)
        records.append(
            {
                "record_schema_version": 1,
                "news_id": news_id,
                "source": "iwencai",
                "publisher": publisher,
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
    records.sort(key=lambda item: (item["published_at"], item["news_id"]))
    bundle_identity = "\0".join(
        (metadata["record_id"], NORMALIZER_VERSION, taxonomy["taxonomy_version"])
    ).encode()
    return {
        "bundle_id": hashlib.sha256(bundle_identity).hexdigest()[:20],
        "records": records,
        "metadata": metadata,
        "taxonomy_version": taxonomy["taxonomy_version"],
    }


def write_bundle(
    built: Dict[str, Any],
    *,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
) -> Path:
    fetched = datetime.fromisoformat(built["metadata"]["fetched_at"])
    destination = normalized_root.joinpath(
        "runs", "iwencai", fetched.strftime("%Y"), fetched.strftime("%m"),
        fetched.strftime("%d"), built["bundle_id"],
    )
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite normalized bundle: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".normalizing-news-", dir=destination.parent))
    try:
        content = (
            "\n".join(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                for item in built["records"]
            )
            + ("\n" if built["records"] else "")
        ).encode()
        with (staging / "news_items.jsonl").open("xb") as handle:
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
                "event_types": sorted({item["event_type"] for item in built["records"]}),
            },
            "table": {
                "logical_name": "news_items",
                "file": "news_items.jsonl",
                "primary_key": ["news_id"],
                "record_count": len(built["records"]),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return destination


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize an archived news response.")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--normalized-root", type=Path, default=DEFAULT_NORMALIZED_ROOT)
    args = parser.parse_args(argv)
    try:
        destination = write_bundle(
            build_news(args.snapshot, taxonomy_path=args.taxonomy),
            normalized_root=args.normalized_root,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
