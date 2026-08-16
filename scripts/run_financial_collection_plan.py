#!/usr/bin/env python3
"""Inspect, resume, and normalize versioned financial collection jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.collect_iwencai_financials import collect_query  # noqa: E402
from scripts.investment_universe import (  # noqa: E402
    InvestmentUniverseError,
    load_investment_universe,
)
from scripts.normalize_iwencai_financials import (  # noqa: E402
    DEFAULT_NORMALIZED_ROOT,
    financial_period_evidence,
    normalize_financial_snapshots,
)
from scripts.save_raw_response import DEFAULT_RAW_ROOT  # noqa: E402


DEFAULT_PLAN = REPOSITORY_ROOT / "config" / "financial_collection_plan.json"


class PlanError(ValueError):
    pass


def load_plan(path: Path = DEFAULT_PLAN) -> Dict[str, Any]:
    path = Path(path)
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or not isinstance(plan.get("jobs"), list):
        raise PlanError("financial collection plan is invalid")
    ids = [job.get("job_id") for job in plan["jobs"]]
    if any(not isinstance(job_id, str) or not job_id for job_id in ids):
        raise PlanError("every job must have a job_id")
    if len(ids) != len(set(ids)):
        raise PlanError("job_id values must be unique")
    known_queries: Dict[str, str] = {}
    for job in plan["jobs"]:
        for name in ("period_end", "purpose", "query"):
            if not isinstance(job.get(name), str) or not job[name]:
                raise PlanError(f"job {job['job_id']} requires {name}")
        try:
            date.fromisoformat(job["period_end"])
        except ValueError as error:
            raise PlanError(
                f"job {job['job_id']} period_end must be YYYY-MM-DD"
            ) from error
        request_version = job.get("request_version", 1)
        if (
            not isinstance(request_version, int)
            or isinstance(request_version, bool)
            or request_version < 1
        ):
            raise PlanError(f"job {job['job_id']} request_version must be positive")
        job["request_version"] = request_version
        minimum_expected_count = job.get("minimum_expected_count")
        if minimum_expected_count is not None and (
            not isinstance(minimum_expected_count, int)
            or isinstance(minimum_expected_count, bool)
            or minimum_expected_count < 1
        ):
            raise PlanError(
                f"job {job['job_id']} minimum_expected_count must be positive"
            )
        aliases = job.get("query_aliases", [])
        if (
            not isinstance(aliases, list)
            or any(not isinstance(query, str) or not query for query in aliases)
            or len(aliases) != len(set(aliases))
        ):
            raise PlanError(f"job {job['job_id']} query_aliases must be unique strings")
        job["query_aliases"] = aliases
        history = job.get("historical_queries", [])
        if not isinstance(history, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("query"), str)
            or not item["query"]
            or not isinstance(item.get("request_version"), int)
            or isinstance(item["request_version"], bool)
            or item["request_version"] < 1
            or item["request_version"] >= request_version
            for item in history
        ):
            raise PlanError(
                f"job {job['job_id']} historical_queries must contain older versioned queries"
            )
        historical_keys = [
            (item["request_version"], item["query"]) for item in history
        ]
        if len(historical_keys) != len(set(historical_keys)):
            raise PlanError(
                f"job {job['job_id']} historical_queries must be unique"
            )
        job["historical_queries"] = history
        for query in [
            job["query"],
            *aliases,
            *(item["query"] for item in history),
        ]:
            owner = known_queries.setdefault(query, job["job_id"])
            if owner != job["job_id"]:
                raise PlanError(
                    f"financial query is assigned to multiple jobs: {owner}, {job['job_id']}"
                )

    universe_path = path.parent / "investment_universe.json"
    if universe_path.is_file():
        try:
            stock_universe = load_investment_universe(universe_path)["stocks"]
        except (InvestmentUniverseError, OSError, TypeError, ValueError) as error:
            raise PlanError(f"investment universe is invalid: {error}") from error
        for job in plan["jobs"]:
            if (
                job.get("request_version", 1) >= 2
                and job.get("universe_id") == stock_universe["universe_id"]
                and job.get("minimum_expected_count")
                != stock_universe["minimum_expected_count"]
            ):
                raise PlanError(
                    f"job {job['job_id']} minimum_expected_count must equal "
                    "the configured stock universe threshold"
                )
    return plan


def _payload_hash(payload: Any) -> str:
    content = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _positive_int(value: Any) -> Optional[int]:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _raw_field_names(metadata: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
    declared = metadata.get("raw_field_names")
    if isinstance(declared, list) and all(
        isinstance(name, str) and name for name in declared
    ):
        return sorted(set(declared))
    fields = set()
    columns = payload.get("columns")
    if isinstance(columns, list):
        for column in columns:
            if isinstance(column, dict):
                name = column.get("key") or column.get("index_name")
                if isinstance(name, str) and name:
                    fields.add(name)
    for key in ("datas", "data"):
        rows = payload.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    fields.update(name for name in row if isinstance(name, str) and name)
    return sorted(fields)


def _pagination_value(
    metadata: Dict[str, Any], payload: Dict[str, Any], field_name: str
) -> Tuple[Optional[int], Optional[str]]:
    request = metadata.get("collection_request")
    if request is not None and not isinstance(request, dict):
        return None, "invalid_collection_request"
    request_value = (
        _positive_int(request.get(field_name)) if isinstance(request, dict) else None
    )
    payload_value = _positive_int(payload.get(field_name))
    if request_value is not None and payload_value is not None and request_value != payload_value:
        return None, f"conflicting_{field_name}"
    return (
        request_value if request_value is not None else payload_value,
        None,
    )


def _matches_job(metadata: Dict[str, Any], job: Dict[str, Any]) -> bool:
    collection_job = metadata.get("collection_job")
    if isinstance(collection_job, dict):
        return (
            collection_job.get("job_id") == job["job_id"]
            and collection_job.get("request_version", 1)
            == job.get("request_version", 1)
        )
    return (
        job.get("request_version", 1) == 1
        and metadata.get("query")
        in [job["query"], *job.get("query_aliases", [])]
    )


def _snapshot_fingerprint(
    *, query: str, raw_field_names: Sequence[str], period_evidence: Dict[str, Any]
) -> str:
    return _payload_hash(
        {
            "query": query,
            "raw_field_names": list(raw_field_names),
            "period_evidence": period_evidence,
        }
    )[:20]


def snapshots_for_job(raw_root: Path, job: Dict[str, Any]) -> List[Dict[str, Any]]:
    matches = []
    source_root = raw_root / "iwencai"
    if not source_root.exists():
        return matches
    for path in source_root.glob("*/*/*/*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        metadata, payload = document.get("metadata"), document.get("payload")
        if not isinstance(metadata, dict) or not isinstance(payload, dict):
            continue
        if not _matches_job(metadata, job):
            continue
        if _payload_hash(payload) != metadata.get("payload_sha256"):
            raise PlanError(f"raw payload hash mismatch: {path}")
        snapshot_errors = []
        page, page_error = _pagination_value(metadata, payload, "page")
        limit, limit_error = _pagination_value(metadata, payload, "limit")
        snapshot_errors.extend(
            error for error in (page_error, limit_error) if error is not None
        )
        total = _positive_int(payload.get("code_count"))
        if page is None or limit is None or total is None:
            snapshot_errors.append("missing_request_pagination")

        rows = payload.get("datas")
        security_codes: List[str] = []
        if not isinstance(rows, list):
            snapshot_errors.append("financial_datas_missing")
            returned_row_count = 0
        else:
            returned_row_count = len(rows)
            for row in rows:
                if not isinstance(row, dict):
                    snapshot_errors.append("financial_row_invalid")
                    continue
                security_code = row.get("股票代码")
                if not isinstance(security_code, str) or not security_code.strip():
                    snapshot_errors.append("financial_security_code_missing")
                    continue
                security_codes.append(security_code.strip())

        query = metadata.get("query")
        if not isinstance(query, str) or not query:
            snapshot_errors.append("missing_query")
            query = ""
        collection_job = metadata.get("collection_job")
        if payload.get("success") is False or (
            payload.get("status_code") is not None
            and payload.get("status_code") != 0
        ):
            snapshot_errors.append("financial_provider_reported_failure")
        if isinstance(collection_job, dict):
            if collection_job.get("expected_period_end") != job["period_end"]:
                snapshot_errors.append("collection_job_period_mismatch")
            if collection_job.get("query_sha256") != _query_hash(query):
                snapshot_errors.append("collection_job_query_hash_mismatch")
            if (
                query != job["query"]
                or collection_job.get("query_sha256") != _query_hash(job["query"])
            ):
                snapshot_errors.append("collection_job_query_drift")

        fields = _raw_field_names(metadata, payload)
        evidence = financial_period_evidence(fields) if fields else {
            "financial_periods": [],
            "report_period_label_periods": [],
            "filing_date_periods": [],
        }
        has_financial_contract = (
            isinstance(collection_job, dict)
            or metadata.get("collector_name") == "hithink-finance-query"
        )
        if has_financial_contract:
            expected = [job["period_end"]]
            if evidence["financial_periods"] != expected:
                snapshot_errors.append("unexpected_financial_period")
            if evidence["report_period_label_periods"] != expected:
                snapshot_errors.append("missing_expected_report_period_label")
            if evidence["filing_date_periods"] != expected:
                snapshot_errors.append("missing_expected_filing_date")
        matches.append(
            {
                "path": path,
                "page": page,
                "limit": limit,
                "total": total,
                "returned_row_count": returned_row_count,
                "security_codes": security_codes,
                "record_id": metadata["record_id"],
                "fetched_at": metadata["fetched_at"],
                "errors": sorted(set(snapshot_errors)),
                "fingerprint": _snapshot_fingerprint(
                    query=query,
                    raw_field_names=fields,
                    period_evidence=evidence,
                ),
            }
        )
    return sorted(
        matches,
        key=lambda item: (
            item["page"] if item["page"] is not None else 0,
            item["fetched_at"],
        ),
    )


def snapshots_for_query(raw_root: Path, query: str) -> List[Dict[str, Any]]:
    """Backward-compatible query lookup for callers outside the plan runner."""
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
        page, page_error = _pagination_value(metadata, payload, "page")
        limit, limit_error = _pagination_value(metadata, payload, "limit")
        total = _positive_int(payload.get("code_count"))
        if page_error or limit_error or page is None or limit is None or total is None:
            raise PlanError(f"snapshot lacks pagination metadata: {path}")
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


def _historical_snapshot_summary(
    raw_root: Path, job: Dict[str, Any]
) -> Dict[str, Any]:
    history = job.get("historical_queries", [])
    if not history:
        return {"snapshot_count": 0, "fingerprints": []}
    historical_pairs = {
        (item["request_version"], item["query"]) for item in history
    }
    historical_queries = {query for _, query in historical_pairs}
    fingerprints = set()
    snapshot_count = 0
    source_root = raw_root / "iwencai"
    if not source_root.exists():
        return {"snapshot_count": 0, "fingerprints": []}
    for path in source_root.glob("*/*/*/*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        metadata, payload = document.get("metadata"), document.get("payload")
        if not isinstance(metadata, dict) or not isinstance(payload, dict):
            continue
        query = metadata.get("query")
        collection_job = metadata.get("collection_job")
        if isinstance(collection_job, dict):
            matched = (
                collection_job.get("job_id") == job["job_id"]
                and (collection_job.get("request_version"), query)
                in historical_pairs
            )
        else:
            matched = query in historical_queries
        if not matched:
            continue
        if _payload_hash(payload) != metadata.get("payload_sha256"):
            raise PlanError(f"raw payload hash mismatch: {path}")
        fields = _raw_field_names(metadata, payload)
        evidence = financial_period_evidence(fields) if fields else {
            "financial_periods": [],
            "report_period_label_periods": [],
            "filing_date_periods": [],
        }
        fingerprints.add(
            _snapshot_fingerprint(
                query=query if isinstance(query, str) else "",
                raw_field_names=fields,
                period_evidence=evidence,
            )
        )
        snapshot_count += 1
    return {
        "snapshot_count": snapshot_count,
        "fingerprints": sorted(fingerprints),
    }


def _unbound_current_snapshot_summary(
    raw_root: Path, job: Dict[str, Any]
) -> Dict[str, Any]:
    if job.get("request_version", 1) == 1:
        return {"snapshot_count": 0, "fingerprints": []}
    active_queries = {job["query"], *job.get("query_aliases", [])}
    fingerprints = set()
    snapshot_count = 0
    source_root = raw_root / "iwencai"
    if not source_root.exists():
        return {"snapshot_count": 0, "fingerprints": []}
    for path in source_root.glob("*/*/*/*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        metadata, payload = document.get("metadata"), document.get("payload")
        if not isinstance(metadata, dict) or not isinstance(payload, dict):
            continue
        if metadata.get("collection_job") is not None:
            continue
        query = metadata.get("query")
        if query not in active_queries:
            continue
        if _payload_hash(payload) != metadata.get("payload_sha256"):
            raise PlanError(f"raw payload hash mismatch: {path}")
        fields = _raw_field_names(metadata, payload)
        evidence = financial_period_evidence(fields) if fields else {
            "financial_periods": [],
            "report_period_label_periods": [],
            "filing_date_periods": [],
        }
        fingerprints.add(
            _snapshot_fingerprint(
                query=query,
                raw_field_names=fields,
                period_evidence=evidence,
            )
        )
        snapshot_count += 1
    return {
        "snapshot_count": snapshot_count,
        "fingerprints": sorted(fingerprints),
    }


def inspect_job(job: Dict[str, Any], *, raw_root: Path = DEFAULT_RAW_ROOT) -> Dict[str, Any]:
    snapshots = snapshots_for_job(raw_root, job)
    historical = _historical_snapshot_summary(raw_root, job)
    unbound_current = _unbound_current_snapshot_summary(raw_root, job)
    snapshot_errors = sorted(
        {error for snapshot in snapshots for error in snapshot["errors"]}
    )
    if unbound_current["snapshot_count"]:
        snapshot_errors.append("collection_job_identity_missing")
    pages: Dict[int, List[Dict[str, Any]]] = {}
    for snapshot in snapshots:
        if snapshot["page"] is not None:
            pages.setdefault(snapshot["page"], []).append(snapshot)
    duplicates = sorted(page for page, items in pages.items() if len(items) > 1)
    totals = sorted(
        {item["total"] for item in snapshots if item["total"] is not None}
    )
    limits = sorted(
        {item["limit"] for item in snapshots if item["limit"] is not None}
    )
    returned_row_count = sum(item["returned_row_count"] for item in snapshots)
    security_codes = [
        security_code
        for item in snapshots
        for security_code in item["security_codes"]
    ]
    unique_security_count = len(set(security_codes))
    errors = list(snapshot_errors)
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
    complete_page_set = (
        expected_pages is not None
        and not duplicates
        and present_pages == list(range(1, expected_pages + 1))
    )
    if len(security_codes) != unique_security_count:
        errors.append("duplicate_security_codes")
    if complete_page_set and returned_row_count != total:
        errors.append("returned_count_mismatch")
    if complete_page_set and unique_security_count != total:
        errors.append("unique_security_count_mismatch")
    minimum_expected_count = job.get("minimum_expected_count")
    if (
        minimum_expected_count is not None
        and any(item_total < minimum_expected_count for item_total in totals)
    ):
        errors.append("below_minimum_expected_count")
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
    quarantined = bool(snapshots or unbound_current["snapshot_count"]) and any(
        error
        in {
            "invalid_collection_request",
            "conflicting_page",
            "conflicting_limit",
            "missing_request_pagination",
            "missing_query",
            "collection_job_period_mismatch",
            "collection_job_query_hash_mismatch",
            "collection_job_query_drift",
            "unexpected_financial_period",
            "missing_expected_report_period_label",
            "missing_expected_filing_date",
            "collection_job_identity_missing",
            "financial_provider_reported_failure",
            "financial_datas_missing",
            "financial_row_invalid",
            "financial_security_code_missing",
            "duplicate_security_codes",
            "returned_count_mismatch",
            "unique_security_count_mismatch",
            "below_minimum_expected_count",
        }
        for error in errors
    )
    if errors:
        next_page = None
    return {
        "job_id": job["job_id"],
        "request_version": job.get("request_version", 1),
        "period_end": job["period_end"],
        "purpose": job["purpose"],
        "status": (
            "quarantined"
            if quarantined
            else "complete"
            if complete
            else "not_started"
            if not snapshots
            else "partial"
        ),
        "snapshot_count": len(snapshots),
        "present_page_count": len(present_pages),
        "present_page_range": (
            [present_pages[0], present_pages[-1]] if present_pages else None
        ),
        "reported_total_count": total,
        "returned_row_count": returned_row_count,
        "unique_security_count": unique_security_count,
        "minimum_expected_count": minimum_expected_count,
        "expected_page_count": expected_pages,
        "next_page": next_page,
        "errors": errors,
        "quarantine_fingerprints": (
            sorted({item["fingerprint"] for item in snapshots})
            if quarantined
            else []
        ),
        "historical_snapshot_count": historical["snapshot_count"],
        "historical_quarantine_fingerprints": historical["fingerprints"],
        "unbound_current_snapshot_count": unbound_current["snapshot_count"],
        "unbound_current_fingerprints": unbound_current["fingerprints"],
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
    page_budget: Optional[int] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    job = _job(plan, job_id)
    status = inspect_job(job, raw_root=raw_root)
    if status["errors"]:
        raise PlanError(f"job cannot resume safely: {status['errors']}")
    if status["status"] == "complete":
        raise PlanError("job is already complete")
    start_page = status["next_page"] or 1
    result = collect_query(
        job["query"],
        start_page=start_page,
        limit=plan["page_limit"],
        page_budget=page_budget,
        timeout=timeout,
        raw_root=raw_root,
        collection_job={
            "collection_job_schema_version": 1,
            "job_id": job["job_id"],
            "request_version": job.get("request_version", 1),
            "expected_period_end": job["period_end"],
            "query_sha256": _query_hash(job["query"]),
        },
    )
    post_collection_status = inspect_job(job, raw_root=raw_root)
    if post_collection_status["errors"]:
        raise PlanError(
            "job Raw was saved but post-collection validation failed: "
            f"{post_collection_status['errors']}"
        )
    return result


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
    collect_parser.add_argument("--page-budget", type=int)
    collect_parser.add_argument("--timeout", type=int, default=60)
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
            result = collect_job(
                plan,
                args.job,
                raw_root=args.raw_root,
                page_budget=args.page_budget,
                timeout=args.timeout,
            )
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
