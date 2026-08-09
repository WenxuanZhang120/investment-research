#!/usr/bin/env python3
"""Resolve monitoring and screening inputs into deterministic offline steps."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.normalize_iwencai_announcements import (  # noqa: E402
    NORMALIZER_VERSION as EVENT_NORMALIZER_VERSION,
    build_events,
)
from scripts.normalize_iwencai_news import (  # noqa: E402
    NORMALIZER_VERSION as NEWS_NORMALIZER_VERSION,
    build_news,
)
from scripts.publish_daily_monitoring_report import (  # noqa: E402
    REPORTER_VERSION,
    monitoring_report_bundle_id,
    monitoring_report_manifest_path,
)
from scripts.repository_paths import repository_relative_path  # noqa: E402
from scripts.screen_market_research_queue import SCREENER_VERSION  # noqa: E402
from scripts.investment_universe import load_investment_universe  # noqa: E402


STREAMS = {
    "announcements": {
        "kind": "events",
        "logical_name": "events",
        "normalizer_script": "scripts/normalize_iwencai_announcements.py",
        "normalizer_version": EVENT_NORMALIZER_VERSION,
        "builder": build_events,
    },
    "news": {
        "kind": "news",
        "logical_name": "news_items",
        "normalizer_script": "scripts/normalize_iwencai_news.py",
        "normalizer_version": NEWS_NORMALIZER_VERSION,
        "builder": build_news,
    },
}


class ResearchReadinessError(ValueError):
    """Raised when research-pipeline readiness is ambiguous or unsafe."""


def _object(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ResearchReadinessError(f"JSON root must be an object: {path}")
    return value


def _repository_path(value: Any, *, repository_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ResearchReadinessError("research readiness paths must be non-empty strings")
    return repository_root / repository_relative_path(
        value, repository_root=repository_root
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ResearchReadinessError(
                    f"JSONL row must be an object: {path}:{line_number}"
                )
            yield value


def _query_log_entries(raw_root: Path) -> List[Dict[str, Any]]:
    entries = []
    seen = set()
    for path in sorted((raw_root / "_query_log").glob("*/*/*.jsonl")):
        for entry in _iter_jsonl(path):
            relative = entry.get("raw_relative_path")
            if not isinstance(relative, str) or not relative:
                raise ResearchReadinessError(f"query log path is missing: {path}")
            if relative in seen:
                raise ResearchReadinessError(
                    f"query log contains duplicate Raw path: {relative}"
                )
            seen.add(relative)
            entries.append(entry)
    return entries


def _routes(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ResearchReadinessError("monitoring_routes must be a non-empty array")
    routes = []
    route_ids = set()
    queries = set()
    for item in value:
        if not isinstance(item, dict):
            raise ResearchReadinessError("monitoring route must be an object")
        route_id, stream, query = (
            item.get("route_id"),
            item.get("stream"),
            item.get("query"),
        )
        if not all(isinstance(field, str) and field for field in (route_id, stream, query)):
            raise ResearchReadinessError("monitoring route fields must be non-empty strings")
        if stream not in STREAMS:
            raise ResearchReadinessError(f"unsupported monitoring stream: {stream}")
        if route_id in route_ids or query in queries:
            raise ResearchReadinessError("monitoring routes must have unique IDs and queries")
        route_ids.add(route_id)
        queries.add(query)
        routes.append({"route_id": route_id, "stream": stream, "query": query})
    return routes


def _validate_normalized_manifest(
    path: Path,
    *,
    built: Dict[str, Any],
    stream: Dict[str, Any],
    taxonomy_version: str,
) -> None:
    manifest = _object(path)
    table = manifest.get("table")
    expected = {
        "bundle_id": built["bundle_id"],
        "normalizer_version": stream["normalizer_version"],
        "taxonomy_version": taxonomy_version,
        "source_raw_record_id": built["metadata"]["record_id"],
    }
    if any(manifest.get(name) != value for name, value in expected.items()):
        raise ResearchReadinessError(
            f"normalized monitoring manifest identity mismatch: {path}"
        )
    if not isinstance(table, dict) or table.get("logical_name") != stream["logical_name"]:
        raise ResearchReadinessError(
            f"normalized monitoring manifest table mismatch: {path}"
        )


def _validate_report_manifest(
    path: Path,
    *,
    bundle_id: str,
    kind: str,
    report_date: str,
    source_manifests: Sequence[str],
) -> None:
    manifest = _object(path)
    actual_sources = manifest.get("source_manifests")
    if not isinstance(actual_sources, list):
        raise ResearchReadinessError(f"monitoring report sources are invalid: {path}")
    actual_paths = [
        item.get("path") for item in actual_sources if isinstance(item, dict)
    ]
    if (
        manifest.get("bundle_id") != bundle_id
        or manifest.get("reporter_version") != REPORTER_VERSION
        or manifest.get("kind") != kind
        or manifest.get("as_of_date") != report_date
        or actual_paths != sorted(source_manifests)
        or manifest.get("investment_judgment_included") is not False
        or manifest.get("automatic_trading_enabled") is not False
    ):
        raise ResearchReadinessError(
            f"monitoring report manifest identity mismatch: {path}"
        )


def _monitoring_steps(
    *,
    routes: Sequence[Dict[str, str]],
    raw_root: Path,
    normalized_root: Path,
    reports_root: Path,
    taxonomy_path: Path,
    repository_root: Path,
    timeout: int,
) -> Dict[str, Any]:
    route_by_query = {item["query"]: item for item in routes}
    taxonomy = _object(taxonomy_path)
    taxonomy_version = taxonomy.get("taxonomy_version")
    if not isinstance(taxonomy_version, str) or not taxonomy_version:
        raise ResearchReadinessError("event taxonomy version is missing")
    raw_root_relative = repository_relative_path(raw_root, repository_root=repository_root)
    normalized_relative = repository_relative_path(
        normalized_root, repository_root=repository_root
    )
    taxonomy_relative = repository_relative_path(
        taxonomy_path, repository_root=repository_root
    )
    reports_relative = repository_relative_path(
        reports_root, repository_root=repository_root
    )
    by_route: Dict[str, List[Dict[str, Any]]] = {
        item["route_id"]: [] for item in routes
    }
    normalization_steps = []
    matched_paths = set()
    for entry in _query_log_entries(raw_root):
        route = route_by_query.get(entry.get("query"))
        if route is None:
            continue
        relative = entry["raw_relative_path"]
        snapshot = raw_root / relative
        if not snapshot.is_file():
            raise ResearchReadinessError(f"monitoring Raw snapshot is missing: {relative}")
        snapshot_public = repository_relative_path(
            snapshot, repository_root=repository_root
        )
        if snapshot_public in matched_paths:
            raise ResearchReadinessError(
                f"monitoring Raw snapshot is routed more than once: {snapshot_public}"
            )
        matched_paths.add(snapshot_public)
        stream = STREAMS[route["stream"]]
        builder: Callable[..., Dict[str, Any]] = stream["builder"]
        built = builder(
            snapshot,
            taxonomy_path=taxonomy_path,
            repository_root=repository_root,
        )
        fetched = datetime.fromisoformat(built["metadata"]["fetched_at"])
        report_date = fetched.date().isoformat()
        manifest_path = normalized_root.joinpath(
            "runs",
            "iwencai",
            fetched.strftime("%Y"),
            fetched.strftime("%m"),
            fetched.strftime("%d"),
            built["bundle_id"],
            "manifest.json",
        )
        manifest_public = repository_relative_path(
            manifest_path, repository_root=repository_root
        )
        status = "planned"
        if manifest_path.exists():
            _validate_normalized_manifest(
                manifest_path,
                built=built,
                stream=stream,
                taxonomy_version=taxonomy_version,
            )
            status = "up_to_date"
        else:
            normalization_steps.append(
                {
                    "step_id": f"normalize_{route['route_id']}_{built['bundle_id']}",
                    "command": [
                        stream["normalizer_script"],
                        snapshot_public,
                        "--taxonomy",
                        taxonomy_relative,
                        "--normalized-root",
                        normalized_relative,
                    ],
                    "timeout_seconds": timeout,
                }
            )
        by_route[route["route_id"]].append(
            {
                "raw_snapshot": snapshot_public,
                "raw_record_id": built["metadata"]["record_id"],
                "bundle_id": built["bundle_id"],
                "normalized_manifest": manifest_public,
                "normalization_status": status,
                "report_date": report_date,
            }
        )

    reporting_steps = []
    route_summaries = []
    for route in routes:
        stream = STREAMS[route["stream"]]
        snapshots = sorted(
            by_route[route["route_id"]],
            key=lambda item: (item["report_date"], item["raw_record_id"]),
        )
        report_summaries = []
        report_dates = sorted({item["report_date"] for item in snapshots})
        for report_date in report_dates:
            group = [item for item in snapshots if item["report_date"] == report_date]
            manifests = sorted(item["normalized_manifest"] for item in group)
            bundle_id = monitoring_report_bundle_id(
                stream["kind"],
                report_date,
                [item["bundle_id"] for item in group],
            )
            report_manifest = monitoring_report_manifest_path(
                reports_root,
                kind=stream["kind"],
                report_date=report_date,
                bundle_id=bundle_id,
            )
            report_public = repository_relative_path(
                report_manifest, repository_root=repository_root
            )
            status = "planned"
            if report_manifest.exists():
                _validate_report_manifest(
                    report_manifest,
                    bundle_id=bundle_id,
                    kind=stream["kind"],
                    report_date=report_date,
                    source_manifests=manifests,
                )
                status = "up_to_date"
            else:
                reporting_steps.append(
                    {
                        "step_id": f"publish_{route['route_id']}_{bundle_id}",
                        "command": [
                            "scripts/publish_daily_monitoring_report.py",
                            stream["kind"],
                            *manifests,
                            "--date",
                            report_date,
                            "--reports-root",
                            reports_relative,
                        ],
                        "timeout_seconds": timeout,
                    }
                )
            report_summaries.append(
                {
                    "as_of_date": report_date,
                    "source_manifest_count": len(manifests),
                    "manifest": report_public,
                    "status": status,
                }
            )
        route_summaries.append(
            {
                "route_id": route["route_id"],
                "stream": route["stream"],
                "query_sha256": hashlib.sha256(route["query"].encode("utf-8")).hexdigest(),
                "matched_snapshot_count": len(snapshots),
                "snapshots": snapshots,
                "reports": report_summaries,
            }
        )
    return {
        "normalization_steps": normalization_steps,
        "reporting_steps": reporting_steps,
        "routes": route_summaries,
        "raw_root": raw_root_relative,
    }


def _market_candidates(
    normalized_root: Path,
    *,
    minimum_universe: int,
) -> List[Dict[str, Any]]:
    candidates = []
    for manifest_path in sorted(normalized_root.glob("runs/**/manifest.json")):
        manifest = _object(manifest_path)
        table = manifest.get("tables", {}).get("valuation_snapshots")
        if not isinstance(table, dict):
            continue
        record_count = table.get("record_count")
        if not isinstance(record_count, int) or record_count < minimum_universe:
            continue
        data_path = manifest_path.parent / table.get("file", "")
        if not data_path.is_file() or _sha256(data_path) != table.get("sha256"):
            raise ResearchReadinessError(f"market valuation table is invalid: {manifest_path}")
        dates = {
            row.get("as_of_date")
            for row in _iter_jsonl(data_path)
            if isinstance(row.get("as_of_date"), str)
        }
        if not dates:
            raise ResearchReadinessError(f"market valuation table has no as_of_date: {data_path}")
        candidates.append(
            {
                "manifest": manifest_path,
                "as_of_date": max(dates),
                "record_count": record_count,
            }
        )
    return candidates


def _metric_candidates(
    derived_root: Path,
    *,
    minimum_universe: int,
) -> List[Dict[str, Any]]:
    candidates = []
    for manifest_path in sorted(derived_root.glob("runs/**/manifest.json")):
        manifest = _object(manifest_path)
        table = manifest.get("table")
        if not isinstance(table, dict) or table.get("logical_name") != "financial_metrics":
            continue
        security_count = manifest.get("coverage", {}).get("security_count")
        if not isinstance(security_count, int) or security_count < minimum_universe:
            continue
        periods = [
            item.get("period_end")
            for item in table.get("partitions", [])
            if isinstance(item, dict) and isinstance(item.get("period_end"), str)
        ]
        if not periods:
            raise ResearchReadinessError(f"financial metric manifest has no periods: {manifest_path}")
        candidates.append(
            {
                "manifest": manifest_path,
                "period_end": max(periods),
                "security_count": security_count,
            }
        )
    return candidates


def _screening_step(
    *,
    normalized_root: Path,
    derived_root: Path,
    rules_path: Path,
    universe_path: Path,
    repository_root: Path,
    minimum_universe: int,
    timeout: int,
) -> Dict[str, Any]:
    market = _market_candidates(
        normalized_root, minimum_universe=minimum_universe
    )
    metrics = _metric_candidates(derived_root, minimum_universe=minimum_universe)
    if not market or not metrics:
        return {
            "status": "waiting_for_full_market_and_metrics",
            "market_manifest": None,
            "metric_manifest": None,
            "derived_manifest": None,
            "step": None,
        }
    selected_market = max(
        market,
        key=lambda item: (item["as_of_date"], item["record_count"], item["manifest"].as_posix()),
    )
    eligible_metrics = [
        item for item in metrics if item["period_end"] <= selected_market["as_of_date"]
    ]
    if not eligible_metrics:
        return {
            "status": "waiting_for_point_in_time_metrics",
            "market_manifest": repository_relative_path(
                selected_market["manifest"], repository_root=repository_root
            ),
            "metric_manifest": None,
            "derived_manifest": None,
            "step": None,
        }
    selected_metrics = max(
        eligible_metrics,
        key=lambda item: (item["period_end"], item["security_count"], item["manifest"].as_posix()),
    )
    rules = _object(rules_path)
    universe = load_investment_universe(universe_path)
    screening_version = rules.get("screening_version")
    if not isinstance(screening_version, str) or not screening_version:
        raise ResearchReadinessError("screening rules version is missing")
    identity = "\0".join(
        (
            SCREENER_VERSION,
            screening_version,
            universe["universe_version"],
            _sha256(selected_market["manifest"]),
            _sha256(selected_metrics["manifest"]),
        )
    ).encode("utf-8")
    bundle_id = hashlib.sha256(identity).hexdigest()[:20]
    as_of = selected_market["as_of_date"]
    destination = derived_root.joinpath(
        "runs",
        "screening",
        as_of[:4],
        as_of[5:7],
        as_of[8:10],
        bundle_id,
        "manifest.json",
    )
    market_public = repository_relative_path(
        selected_market["manifest"], repository_root=repository_root
    )
    metric_public = repository_relative_path(
        selected_metrics["manifest"], repository_root=repository_root
    )
    destination_public = repository_relative_path(
        destination, repository_root=repository_root
    )
    if destination.exists():
        manifest = _object(destination)
        table = manifest.get("table")
        if (
            manifest.get("bundle_id") != bundle_id
            or manifest.get("screener_version") != SCREENER_VERSION
            or manifest.get("screening_version") != screening_version
            or manifest.get("universe_version") != universe["universe_version"]
            or manifest.get("universe_id") != universe["stocks"]["universe_id"]
            or manifest.get("source_market_manifest") != market_public
            or manifest.get("source_metric_manifest") != metric_public
            or not isinstance(table, dict)
            or table.get("logical_name") != "market_research_queue"
        ):
            raise ResearchReadinessError(
                f"screening manifest identity mismatch: {destination}"
            )
        status = "up_to_date"
        step = None
    else:
        status = "planned"
        step = {
            "step_id": f"screen_market_{bundle_id}",
            "command": [
                "scripts/screen_market_research_queue.py",
                market_public,
                metric_public,
                "--rules",
                repository_relative_path(rules_path, repository_root=repository_root),
                "--universe",
                repository_relative_path(
                    universe_path, repository_root=repository_root
                ),
                "--derived-root",
                repository_relative_path(derived_root, repository_root=repository_root),
            ],
            "timeout_seconds": timeout,
        }
    return {
        "status": status,
        "as_of_date": as_of,
        "financial_period_end": selected_metrics["period_end"],
        "market_manifest": market_public,
        "metric_manifest": metric_public,
        "derived_manifest": destination_public,
        "step": step,
    }


def resolve_research_pipeline(
    settings: Dict[str, Any],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    """Return deterministic steps and portable readiness evidence."""
    if not isinstance(settings, dict) or settings.get("schema_version") != 1:
        raise ResearchReadinessError("research_readiness schema_version must be 1")
    repository_root = Path(repository_root).resolve()
    raw_root = _repository_path(settings.get("raw_root"), repository_root=repository_root)
    normalized_root = _repository_path(
        settings.get("normalized_root"), repository_root=repository_root
    )
    derived_root = _repository_path(
        settings.get("derived_root"), repository_root=repository_root
    )
    reports_root = _repository_path(
        settings.get("reports_root"), repository_root=repository_root
    )
    taxonomy_path = _repository_path(
        settings.get("event_taxonomy"), repository_root=repository_root
    )
    rules_path = _repository_path(
        settings.get("screening_rules"), repository_root=repository_root
    )
    universe_path = _repository_path(
        settings.get("investment_universe"), repository_root=repository_root
    )
    routes = _routes(settings.get("monitoring_routes"))
    timeout = settings.get("timeout_seconds", 300)
    minimum_universe = settings.get("minimum_screening_universe", 5000)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ResearchReadinessError("research timeout_seconds must be positive")
    if (
        not isinstance(minimum_universe, int)
        or isinstance(minimum_universe, bool)
        or minimum_universe <= 0
    ):
        raise ResearchReadinessError(
            "minimum_screening_universe must be a positive integer"
        )
    monitoring = _monitoring_steps(
        routes=routes,
        raw_root=raw_root,
        normalized_root=normalized_root,
        reports_root=reports_root,
        taxonomy_path=taxonomy_path,
        repository_root=repository_root,
        timeout=timeout,
    )
    screening = _screening_step(
        normalized_root=normalized_root,
        derived_root=derived_root,
        rules_path=rules_path,
        universe_path=universe_path,
        repository_root=repository_root,
        minimum_universe=minimum_universe,
        timeout=timeout,
    )
    derivation_steps = [screening["step"]] if screening["step"] else []
    planned_count = (
        len(monitoring["normalization_steps"])
        + len(monitoring["reporting_steps"])
        + len(derivation_steps)
    )
    matched_count = sum(
        route["matched_snapshot_count"] for route in monitoring["routes"]
    )
    waiting = matched_count == 0 or screening["status"].startswith("waiting_")
    status = (
        "work_planned"
        if planned_count
        else ("waiting_for_inputs" if waiting else "up_to_date")
    )
    return {
        "schema_version": 1,
        "status": status,
        "planned_step_count": planned_count,
        "normalization_steps": monitoring["normalization_steps"],
        "derivation_steps": derivation_steps,
        "reporting_steps": monitoring["reporting_steps"],
        "monitoring": {
            "matched_snapshot_count": matched_count,
            "routes": monitoring["routes"],
        },
        "screening": {name: value for name, value in screening.items() if name != "step"},
    }
