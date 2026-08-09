#!/usr/bin/env python3
"""Publish immutable, lineage-rich daily event or news report bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.generate_daily_event_report import load_events, render_report as render_events  # noqa: E402
from scripts.generate_daily_news_report import load_news, render_report as render_news  # noqa: E402
from scripts.repository_paths import repository_relative_path  # noqa: E402


DEFAULT_REPORTS_ROOT = REPOSITORY_ROOT / "reports" / "daily"
REPORTER_VERSION = "1.0.0"
KINDS = {
    "events": {
        "logical_name": "events",
        "report_filename": "announcement-events.md",
        "purpose": "factual_announcement_index_no_investment_judgment",
    },
    "news": {
        "logical_name": "news_items",
        "report_filename": "financial-news.md",
        "purpose": "factual_news_index_no_investment_judgment",
    },
}


class MonitoringReportError(ValueError):
    """Raised when an audited monitoring report cannot be built safely."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def monitoring_report_bundle_id(
    kind: str,
    report_date: str,
    source_bundle_ids: Sequence[str],
) -> str:
    """Return the deterministic identity shared by resolver and publisher."""
    if kind not in KINDS:
        raise MonitoringReportError(f"unsupported monitoring report kind: {kind}")
    date.fromisoformat(report_date)
    if not source_bundle_ids or any(
        not isinstance(value, str) or not value for value in source_bundle_ids
    ):
        raise MonitoringReportError("source bundle IDs must be non-empty strings")
    if len(source_bundle_ids) != len(set(source_bundle_ids)):
        raise MonitoringReportError("source bundle IDs must not contain duplicates")
    identity = "\0".join(
        [REPORTER_VERSION, kind, report_date, *sorted(source_bundle_ids)]
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:20]


def monitoring_report_manifest_path(
    reports_root: Path,
    *,
    kind: str,
    report_date: str,
    bundle_id: str,
) -> Path:
    parsed = date.fromisoformat(report_date)
    return Path(reports_root).joinpath(
        "monitoring",
        kind,
        parsed.strftime("%Y"),
        parsed.strftime("%m"),
        parsed.strftime("%d"),
        bundle_id,
        "manifest.json",
    )


def _read_manifest(
    path: Path,
    *,
    kind: str,
    repository_root: Path,
) -> Dict[str, Any]:
    repository_relative_path(path, repository_root=repository_root)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise MonitoringReportError(f"manifest root must be an object: {path}")
    table = document.get("table")
    expected = KINDS[kind]["logical_name"]
    if not isinstance(table, dict) or table.get("logical_name") != expected:
        raise MonitoringReportError(f"manifest is not {expected}: {path}")
    bundle_id = document.get("bundle_id")
    if not isinstance(bundle_id, str) or not bundle_id:
        raise MonitoringReportError(f"manifest bundle_id is missing: {path}")
    return document


def build_monitoring_report(
    kind: str,
    manifests: Sequence[Path],
    *,
    report_date: str,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    """Validate sources and render one deterministic factual report."""
    if kind not in KINDS:
        raise MonitoringReportError(f"unsupported monitoring report kind: {kind}")
    date.fromisoformat(report_date)
    repository_root = Path(repository_root).resolve()
    supplied = [Path(path) for path in manifests]
    if not supplied:
        raise MonitoringReportError("at least one source manifest is required")
    public_paths = [
        repository_relative_path(path, repository_root=repository_root)
        for path in supplied
    ]
    if len(public_paths) != len(set(public_paths)):
        raise MonitoringReportError("source manifests must not contain duplicates")
    ordered = sorted(zip(public_paths, supplied), key=lambda item: item[0])
    source_manifests: List[Dict[str, Any]] = []
    ordered_paths = []
    for public_path, path in ordered:
        document = _read_manifest(
            path, kind=kind, repository_root=repository_root
        )
        source_manifests.append(
            {
                "path": public_path,
                "sha256": _sha256(path),
                "bundle_id": document["bundle_id"],
                "source_raw_record_id": document.get("source_raw_record_id"),
                "fetched_at": document.get("fetched_at"),
            }
        )
        ordered_paths.append(path)

    if kind == "events":
        records = load_events(ordered_paths)
        content = render_events(records, report_date)
    else:
        records = load_news(ordered_paths)
        content = render_news(records, report_date)
    content_bytes = content.encode("utf-8")
    source_ids = [item["bundle_id"] for item in source_manifests]
    bundle_id = monitoring_report_bundle_id(kind, report_date, source_ids)
    published = sorted(
        item["published_at"]
        for item in records
        if isinstance(item.get("published_at"), str)
    )
    available = sorted(
        item["available_from"]
        for item in records
        if isinstance(item.get("available_from"), str)
    )
    report_filename = KINDS[kind]["report_filename"]
    manifest = {
        "monitoring_report_schema_version": 1,
        "bundle_id": bundle_id,
        "reporter_version": REPORTER_VERSION,
        "kind": kind,
        "purpose": KINDS[kind]["purpose"],
        "as_of_date": report_date,
        "source_manifests": source_manifests,
        "coverage": {
            "record_count": len(records),
            "published_at_min": published[0] if published else None,
            "published_at_max": published[-1] if published else None,
            "available_from_max": available[-1] if available else None,
        },
        "report": {
            "file": report_filename,
            "sha256": hashlib.sha256(content_bytes).hexdigest(),
        },
        "investment_judgment_included": False,
        "automatic_trading_enabled": False,
    }
    return {"manifest": manifest, "content": content_bytes}


def _expected_files(built: Dict[str, Any]) -> Dict[str, bytes]:
    manifest = built["manifest"]
    return {
        manifest["report"]["file"]: built["content"],
        "manifest.json": (
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2)
            + "\n"
        ).encode("utf-8"),
    }


def write_monitoring_report(
    built: Dict[str, Any],
    *,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    """Write or verify one immutable versioned report bundle."""
    repository_root = Path(repository_root).resolve()
    reports_root = repository_root / repository_relative_path(
        reports_root, repository_root=repository_root
    )
    manifest = built["manifest"]
    destination_manifest = monitoring_report_manifest_path(
        reports_root,
        kind=manifest["kind"],
        report_date=manifest["as_of_date"],
        bundle_id=manifest["bundle_id"],
    )
    destination = destination_manifest.parent
    repository_relative_path(destination, repository_root=repository_root)
    expected = _expected_files(built)
    if destination.exists():
        if not destination.is_dir():
            raise MonitoringReportError("monitoring report destination is not a directory")
        actual_names = sorted(path.name for path in destination.iterdir() if path.is_file())
        if actual_names != sorted(expected):
            raise MonitoringReportError("existing monitoring report bundle is incomplete")
        for name, content in expected.items():
            if (destination / name).read_bytes() != content:
                raise MonitoringReportError(
                    f"existing monitoring report differs: {name}"
                )
        return destination_manifest

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".monitoring-report-", dir=destination.parent)
    )
    try:
        for name, content in expected.items():
            with (staging / name).open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return destination_manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish an immutable, audited event or news report bundle."
    )
    parser.add_argument("kind", choices=sorted(KINDS))
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--reports-root", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    reports_root = args.reports_root or root / "reports/daily"
    try:
        destination = write_monitoring_report(
            build_monitoring_report(
                args.kind,
                args.manifests,
                report_date=args.date,
                repository_root=root,
            ),
            reports_root=reports_root,
            repository_root=root,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(repository_relative_path(destination, repository_root=root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
