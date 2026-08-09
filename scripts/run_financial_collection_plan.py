#!/usr/bin/env python3
"""Inspect, resume, and normalize versioned financial collection jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.collect_iwencai_financials import collect_query  # noqa: E402
from scripts.normalize_iwencai_financials import (  # noqa: E402
    DEFAULT_NORMALIZED_ROOT,
    normalize_financial_snapshots,
)
from scripts.save_raw_response import DEFAULT_RAW_ROOT  # noqa: E402


DEFAULT_PLAN = REPOSITORY_ROOT / "config" / "financial_collection_plan.json"


class PlanError(ValueError):
    pass


def load_plan(path: Path = DEFAULT_PLAN) -> Dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or not isinstance(plan.get("jobs"), list):
        raise PlanError("financial collection plan is invalid")
    ids = [job.get("job_id") for job in plan["jobs"]]
    if any(not isinstance(job_id, str) or not job_id for job_id in ids):
        raise PlanError("every job must have a job_id")
    if len(ids) != len(set(ids)):
        raise PlanError("job_id values must be unique")
    return plan


def _payload_hash(payload: Any) -> str:
    content = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(content).hexdigest()


def snapshots_for_query(raw_root: Path, query: str) -> List[Dict[str, Any]]:
    matches = []
    source_root = raw_root / "iwencai"
    if not source_root.exists():
        return matches
    for path in source_root.glob("*/*/*/*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        metadata, payload = document.get("metadata"), document.get("payload")
        if not isinstance(metadata, dict) or not isinstance(payload, dict):
            continue
        if metadata.get("query") != query:
            continue
        if _payload_hash(payload) != metadata.get("payload_sha256"):
            raise PlanError(f"raw payload hash mismatch: {path}")
        try:
            page = int(payload["page"])
            limit = int(payload["limit"])
            total = int(payload["code_count"])
        except (KeyError, TypeError, ValueError) as error:
            raise PlanError(f"snapshot lacks pagination metadata: {path}") from error
        matches.append(
            {
                "path": path,
                "page": page,
                "limit": limit,
                "total": total,
                "record_id": metadata["record_id"],
                "fetched_at": metadata["fetched_at"],
            }
        )
    return sorted(matches, key=lambda item: (item["page"], item["fetched_at"]))


def inspect_job(job: Dict[str, Any], *, raw_root: Path = DEFAULT_RAW_ROOT) -> Dict[str, Any]:
    snapshots = snapshots_for_query(raw_root, job["query"])
    pages: Dict[int, List[Dict[str, Any]]] = {}
    for snapshot in snapshots:
        pages.setdefault(snapshot["page"], []).append(snapshot)
    duplicates = sorted(page for page, items in pages.items() if len(items) > 1)
    totals = sorted({item["total"] for item in snapshots})
    limits = sorted({item["limit"] for item in snapshots})
    errors = []
    if duplicates:
        errors.append("duplicate_pages")
    if len(totals) > 1:
        errors.append("inconsistent_total")
    if len(limits) > 1:
        errors.append("inconsistent_limit")
    total = totals[0] if len(totals) == 1 else None
    limit = limits[0] if len(limits) == 1 else None
    expected_pages = math.ceil(total / limit) if total is not None and limit else None
    present_pages = sorted(pages)
    contiguous_tail = present_pages == list(range(1, len(present_pages) + 1))
    if present_pages and not contiguous_tail:
        errors.append("non_contiguous_pages")
    next_page = len(present_pages) + 1 if contiguous_tail else None
    complete = (
        not errors
        and expected_pages is not None
        and present_pages == list(range(1, expected_pages + 1))
    )
    if complete:
        next_page = None
    return {
        "job_id": job["job_id"],
        "period_end": job["period_end"],
        "purpose": job["purpose"],
        "status": "complete" if complete else ("not_started" if not snapshots else "partial"),
        "snapshot_count": len(snapshots),
        "present_page_count": len(present_pages),
        "present_page_range": (
            [present_pages[0], present_pages[-1]] if present_pages else None
        ),
        "reported_total_count": total,
        "expected_page_count": expected_pages,
        "next_page": next_page,
        "errors": errors,
        "snapshot_paths": [str(pages[page][0]["path"]) for page in present_pages if len(pages[page]) == 1],
    }


def inspect_plan(
    plan: Dict[str, Any],
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    jobs = [job for job in plan["jobs"] if job_id is None or job["job_id"] == job_id]
    if job_id and not jobs:
        raise PlanError(f"unknown job_id: {job_id}")
    statuses = [inspect_job(job, raw_root=raw_root) for job in jobs]
    public_statuses = [
        {key: value for key, value in status.items() if key != "snapshot_paths"}
        for status in statuses
    ]
    return {
        "plan_version": plan["plan_version"],
        "all_collections_complete": bool(statuses)
        and all(item["status"] == "complete" for item in statuses),
        "jobs": public_statuses,
    }


def _job(plan: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    matches = [job for job in plan["jobs"] if job["job_id"] == job_id]
    if not matches:
        raise PlanError(f"unknown job_id: {job_id}")
    return matches[0]


def collect_job(
    plan: Dict[str, Any],
    job_id: str,
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
) -> Dict[str, Any]:
    job = _job(plan, job_id)
    status = inspect_job(job, raw_root=raw_root)
    if status["errors"]:
        raise PlanError(f"job cannot resume safely: {status['errors']}")
    if status["status"] == "complete":
        raise PlanError("job is already complete")
    start_page = status["next_page"] or 1
    return collect_query(
        job["query"],
        start_page=start_page,
        limit=plan["page_limit"],
        raw_root=raw_root,
    )


def normalize_job(
    plan: Dict[str, Any],
    job_id: str,
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
    normalized_root: Path = DEFAULT_NORMALIZED_ROOT,
) -> Path:
    job = _job(plan, job_id)
    status = inspect_job(job, raw_root=raw_root)
    if status["status"] != "complete" or status["errors"]:
        raise PlanError("job must be complete and error-free before normalization")
    return normalize_financial_snapshots(
        [Path(path) for path in status["snapshot_paths"]],
        normalized_root=normalized_root,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the versioned financial collection plan.")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument(
        "--normalized-root", type=Path, default=DEFAULT_NORMALIZED_ROOT
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--job")
    status_parser.add_argument("--require-complete", action="store_true")
    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--job", required=True)
    normalize_parser = subparsers.add_parser("normalize")
    normalize_parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan)
        if args.command == "status":
            result = inspect_plan(plan, raw_root=args.raw_root, job_id=args.job)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return (
                1
                if args.require_complete
                and not result["all_collections_complete"]
                else 0
            )
        if args.command == "collect":
            result = collect_job(plan, args.job, raw_root=args.raw_root)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        destination = normalize_job(
            plan,
            args.job,
            raw_root=args.raw_root,
            normalized_root=args.normalized_root,
        )
        print(destination)
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
