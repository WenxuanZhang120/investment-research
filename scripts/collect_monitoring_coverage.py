#!/usr/bin/env python3
"""Run the five configured monitoring queries and audit gateway coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.import_codex_collection import extract_raw_field_names  # noqa: E402
from scripts.normalize_iwencai_announcements import _timestamp  # noqa: E402
from scripts.normalize_iwencai_news import _publisher  # noqa: E402
from scripts.repository_paths import repository_relative_path  # noqa: E402
from scripts.resolve_monitoring_collection_scope import (  # noqa: E402
    resolve_monitoring_jobs,
)
from scripts.save_raw_response import (  # noqa: E402
    DEFAULT_RAW_ROOT,
    PROJECT_TIMEZONE,
    save_raw_response,
)


COVERAGE_VERSION = "1.1.0"
DEFAULT_REPORTS_ROOT = REPOSITORY_ROOT / "reports" / "daily"


class MonitoringCoverageError(RuntimeError):
    """Raised when a live monitoring query cannot be tested safely."""


def _call_skill(
    script: Path,
    *,
    query: str,
    size: int,
    timeout: int,
) -> Dict[str, Any]:
    script = Path(script)
    if not script.is_file():
        raise MonitoringCoverageError(f"skill script does not exist: {script.name}")
    with tempfile.TemporaryDirectory(prefix="iwencai-coverage-") as temporary:
        output = Path(temporary) / "raw-response.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                query,
                "--size",
                str(size),
                "--timeout",
                str(timeout),
                "--output",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 10,
            check=False,
            env=dict(os.environ),
        )
        if completed.returncode != 0:
            raise MonitoringCoverageError(
                f"{script.name} exited with code {completed.returncode}"
            )
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MonitoringCoverageError(
                f"{script.name} did not produce a JSON response"
            ) from error
    if not isinstance(payload, dict):
        raise MonitoringCoverageError("gateway response root must be an object")
    return payload


def _source_codes(item: Dict[str, Any]) -> set[str]:
    stock_infos = item.get("stock_infos")
    if not isinstance(stock_infos, list):
        return set()
    return {
        stock["code"].split(".", 1)[0]
        for stock in stock_infos
        if isinstance(stock, dict)
        and isinstance(stock.get("code"), str)
        and stock["code"]
    }


def summarize_payload(
    payload: Dict[str, Any],
    *,
    requested_size: int,
    scope: Dict[str, Any],
) -> Dict[str, Any]:
    data = payload.get("data")
    rows = data if isinstance(data, list) else []
    total = payload.get("total")
    total = total if isinstance(total, int) and not isinstance(total, bool) else None
    publishers = sorted(
        {
            publisher
            for item in rows
            if isinstance(item, dict)
            for publisher in [_publisher(item)]
            if publisher
        }
    )
    domains = sorted(
        {
            urlparse(item["url"]).netloc.lower()
            for item in rows
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and urlparse(item["url"]).netloc
        }
    )
    published = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            published.append(_timestamp(item.get("publish_time")))
        except (TypeError, ValueError):
            continue
    targets = {
        code.split(".", 1)[0]
        for code in scope.get("target_security_codes", [])
        if isinstance(code, str)
    }
    matched_target_items = 0
    off_scope_items = 0
    unidentified_security_items = 0
    if scope.get("scope_type") == "p0_securities":
        for item in rows:
            codes = _source_codes(item) if isinstance(item, dict) else set()
            if not codes:
                unidentified_security_items += 1
            elif codes.intersection(targets):
                matched_target_items += 1
            else:
                off_scope_items += 1
    returned = len(rows)
    return {
        "gateway_status_code": payload.get("status_code"),
        "gateway_status_message": payload.get("status_msg"),
        "requested_size": requested_size,
        "returned_count": returned,
        "reported_total": total,
        "truncated_within_reported_total": total is not None and total > returned,
        "requested_size_honored": (
            total is None or returned >= min(requested_size, total)
        ),
        "publisher_count": len(publishers),
        "publishers": publishers,
        "source_domains": domains,
        "published_at_min": min(published) if published else None,
        "published_at_max": max(published) if published else None,
        "target_security_count": len(targets),
        "matched_target_items": matched_target_items,
        "off_scope_items": off_scope_items,
        "unidentified_security_items": unidentified_security_items,
    }


def assess_coverage(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Separate successful API execution from the quality of returned coverage."""
    gaps = []
    for item in results:
        task_id = item.get("task_id") or "unknown_task"
        if item.get("status") != "succeeded":
            gaps.append(f"{task_id}: query execution failed")
            continue
        if item.get("truncated_within_reported_total") is True:
            gaps.append(
                f"{task_id}: returned {item.get('returned_count')} of "
                f"{item.get('reported_total')} reported results"
            )
        scope = item.get("collection_scope")
        if not isinstance(scope, dict) or scope.get("scope_type") != "p0_securities":
            continue
        unidentified = item.get("unidentified_security_items")
        if isinstance(unidentified, int) and unidentified > 0:
            gaps.append(
                f"{task_id}: {unidentified} returned items have no security identity"
            )
        returned = item.get("returned_count")
        matched = item.get("matched_target_items")
        if (
            isinstance(returned, int)
            and returned > 0
            and isinstance(matched, int)
            and matched == 0
        ):
            gaps.append(f"{task_id}: no returned item is attributable to a P0 target")
    return {
        "coverage_status": "insufficient" if gaps else "reported_results_complete",
        "coverage_gaps": gaps,
        "reported_result_coverage_complete": not gaps,
    }


def _write_report(
    report: Dict[str, Any],
    *,
    reports_root: Path,
    repository_root: Path,
) -> Path:
    started = datetime.fromisoformat(report["started_at"])
    report_id = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(reports_root).joinpath(
        "news-coverage",
        started.strftime("%Y"),
        started.strftime("%m"),
        started.strftime("%d"),
        report_id,
        "新闻资讯覆盖验收.json",
    )
    repository_relative_path(destination, repository_root=repository_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write("\n")
    return destination


def collect_monitoring_coverage(
    *,
    news_script: Path,
    announcement_script: Path,
    repository_root: Path = REPOSITORY_ROOT,
    raw_root: Optional[Path] = None,
    reports_root: Optional[Path] = None,
    run_date: Optional[str] = None,
    timeout: int = 30,
    jobs: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    repository_root = Path(repository_root)
    raw_root = Path(raw_root) if raw_root is not None else repository_root / "data/raw"
    reports_root = (
        Path(reports_root)
        if reports_root is not None
        else repository_root / "reports/daily"
    )
    repository_relative_path(raw_root, repository_root=repository_root)
    repository_relative_path(reports_root, repository_root=repository_root)
    if timeout < 1:
        raise MonitoringCoverageError("timeout must be positive")
    resolved = None
    if jobs is None:
        resolved = resolve_monitoring_jobs(
            repository_root=repository_root,
            derived_root=repository_root / "data/derived",
            universe_path=repository_root / "config/investment_universe.json",
            plan_path=repository_root / "config/codex_daily_collection.json",
            run_date=run_date,
        )
        jobs = resolved["jobs"]
    started = datetime.now(PROJECT_TIMEZONE)
    results: List[Dict[str, Any]] = []
    overall_status = "succeeded"
    for job in jobs:
        script = (
            announcement_script
            if job["dataset_kind"] == "announcements"
            else news_script
        )
        try:
            payload = _call_skill(
                script,
                query=job["query"],
                size=job["requested_result_count"],
                timeout=timeout,
            )
            try:
                fields = extract_raw_field_names(payload)
            except ValueError:
                fields = None
            snapshot = save_raw_response(
                payload,
                source="iwencai",
                query=job["query"],
                raw_root=raw_root,
                as_of_date=job["as_of_date"],
                raw_field_names=fields,
                collection_method="codex_agent",
                collector_name=job["tool"],
                collection_scope=job["collection_scope"],
            )
            summary = summarize_payload(
                payload,
                requested_size=job["requested_result_count"],
                scope=job["collection_scope"],
            )
            result = {
                "task_id": job["task_id"],
                "collection_id": job["collection_id"],
                "dataset_kind": job["dataset_kind"],
                "collector": job["tool"],
                "query": job["query"],
                "collection_scope": job["collection_scope"],
                "raw_snapshot": repository_relative_path(
                    snapshot, repository_root=repository_root
                ),
                **summary,
            }
            if summary["gateway_status_code"] != 0 or not isinstance(
                payload.get("data"), list
            ):
                overall_status = "failed"
                result["status"] = "failed"
                results.append(result)
                break
            result["status"] = "succeeded"
            results.append(result)
        except (
            MonitoringCoverageError,
            OSError,
            subprocess.SubprocessError,
            TypeError,
            ValueError,
        ) as error:
            overall_status = "failed"
            results.append(
                {
                    "task_id": job.get("task_id"),
                    "collection_id": job.get("collection_id"),
                    "dataset_kind": job.get("dataset_kind"),
                    "collector": job.get("tool"),
                    "status": "failed",
                    "error": str(error),
                }
            )
            break
    finished = datetime.now(PROJECT_TIMEZONE)
    coverage = assess_coverage(results)
    report = {
        "coverage_validation_version": COVERAGE_VERSION,
        "status": overall_status,
        "source": "同花顺问财",
        "started_at": started.isoformat(timespec="microseconds"),
        "finished_at": finished.isoformat(timespec="microseconds"),
        "run_date": run_date or started.date().isoformat(),
        "configured_job_count": len(jobs),
        "executed_job_count": len(results),
        "p0_target_count": resolved["p0_target_count"] if resolved else None,
        "p0_source_manifest": resolved["p0_source_manifest"] if resolved else None,
        "results": results,
        "all_connections_ok": overall_status == "succeeded",
        "all_requested_sizes_honored": overall_status == "succeeded" and all(
            item.get("requested_size_honored") is True for item in results
        ),
        "all_reported_totals_returned": overall_status == "succeeded" and all(
            item.get("truncated_within_reported_total") is False for item in results
        ),
        **coverage,
        "search_exhaustiveness_guaranteed": False,
        "investment_judgment_included": False,
        "automatic_trading_enabled": False,
        "credential_value_persisted": False,
    }
    report_path = _write_report(
        report,
        reports_root=reports_root,
        repository_root=repository_root,
    )
    return {
        **report,
        "report_path": repository_relative_path(
            report_path, repository_root=repository_root
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="执行五条真实资讯查询，Raw-first 保存并检查返回完整性。"
    )
    parser.add_argument("--news-script", type=Path, required=True)
    parser.add_argument("--announcement-script", type=Path, required=True)
    parser.add_argument("--date", dest="run_date")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    try:
        result = collect_monitoring_coverage(
            news_script=args.news_script,
            announcement_script=args.announcement_script,
            repository_root=args.root,
            run_date=args.run_date,
            timeout=args.timeout,
        )
    except (MonitoringCoverageError, OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    if result["status"] != "succeeded":
        return 1
    return 0 if result["reported_result_coverage_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
