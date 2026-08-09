#!/usr/bin/env python3
"""Preflight or run one explicitly authorized financial collection job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.repository_paths import repository_relative_path  # noqa: E402
from scripts.run_financial_collection_plan import (  # noqa: E402
    DEFAULT_PLAN,
    collect_job,
    inspect_job,
    load_plan,
)
from scripts.save_raw_response import DEFAULT_RAW_ROOT  # noqa: E402


DEFAULT_POLICY = REPOSITORY_ROOT / "config" / "collection_safety.json"
ACTIONS = ("preflight", "collect")


class GuardedCollectionError(ValueError):
    """Raised when collection authorization or artifact handling is unsafe."""


def _read_object(path: Path) -> Dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise GuardedCollectionError(f"JSON root must be an object: {path}")
    return document


def load_policy(path: Path = DEFAULT_POLICY) -> Dict[str, Any]:
    policy = _read_object(path)
    required_strings = (
        "policy_version",
        "source",
        "credential_environment_variable",
        "confirmation_prefix",
    )
    for name in required_strings:
        if not isinstance(policy.get(name), str) or not policy[name]:
            raise GuardedCollectionError(f"collection policy requires {name}")
    allowed = policy.get("allowed_job_ids")
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(not isinstance(item, str) or not item for item in allowed)
        or len(allowed) != len(set(allowed))
    ):
        raise GuardedCollectionError("allowed_job_ids must be a unique string array")
    for name in ("max_pages_per_run", "request_timeout_seconds"):
        value = policy.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise GuardedCollectionError(f"collection policy requires positive {name}")
    return policy


def _public_status(status: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in status.items() if key != "snapshot_paths"}


def build_preflight(
    plan: Dict[str, Any],
    policy: Dict[str, Any],
    *,
    job_id: str,
    action: str,
    confirmation: str,
    raw_root: Path,
    credential_present: bool,
) -> Dict[str, Any]:
    if action not in ACTIONS:
        raise GuardedCollectionError(f"unsupported action: {action}")
    jobs = {job["job_id"]: job for job in plan["jobs"]}
    job = jobs.get(job_id)
    if job is None:
        raise GuardedCollectionError(f"unknown job_id: {job_id}")
    status = inspect_job(job, raw_root=raw_root)
    required_confirmation = policy["confirmation_prefix"] + job_id
    confirmation_valid = secrets.compare_digest(
        confirmation or "", required_confirmation
    )
    data_reasons = []
    if job_id not in policy["allowed_job_ids"]:
        data_reasons.append("job_not_allowed_by_policy")
    if status["errors"]:
        data_reasons.append("collection_state_has_errors")
    if status["status"] == "complete":
        data_reasons.append("job_already_complete")
    if status["status"] != "complete" and status["next_page"] is None:
        data_reasons.append("next_page_is_not_safe")

    remaining_pages = None
    if status["expected_page_count"] is not None and status["next_page"] is not None:
        remaining_pages = status["expected_page_count"] - status["next_page"] + 1
    planned_page_count = (
        min(policy["max_pages_per_run"], remaining_pages)
        if remaining_pages is not None
        else policy["max_pages_per_run"]
    )
    authorization_reasons = []
    if action == "collect" and not confirmation_valid:
        authorization_reasons.append("confirmation_mismatch")
    if action == "collect" and not credential_present:
        authorization_reasons.append("credential_missing")
    reasons = data_reasons + authorization_reasons
    return {
        "preflight_schema_version": 1,
        "policy_version": policy["policy_version"],
        "plan_version": plan["plan_version"],
        "source": policy["source"],
        "job_id": job_id,
        "period_end": job["period_end"],
        "purpose": job["purpose"],
        "requested_action": action,
        "required_confirmation": required_confirmation,
        "confirmation_valid": confirmation_valid,
        "credential_environment_variable": policy[
            "credential_environment_variable"
        ],
        "credential_present": credential_present,
        "ready_for_collection_request": not data_reasons,
        "collection_allowed": action == "collect" and not reasons,
        "planned_start_page": status["next_page"],
        "planned_page_count": planned_page_count,
        "known_remaining_page_count": remaining_pages,
        "max_pages_per_run": policy["max_pages_per_run"],
        "request_timeout_seconds": policy["request_timeout_seconds"],
        "blocked_reasons": reasons,
        "job_status_before": _public_status(status),
    }


def _snapshot_paths(raw_root: Path) -> Set[Path]:
    source_root = raw_root / "iwencai"
    if not source_root.exists():
        return set()
    return {path.resolve() for path in source_root.glob("*/*/*/*.json")}


def _copy_collection_evidence(
    staging: Path,
    *,
    raw_root: Path,
    snapshot_paths: Sequence[Path],
    repository_root: Path,
) -> Tuple[List[str], List[str]]:
    public_snapshots = []
    query_logs = set()
    for source in sorted(snapshot_paths):
        public_path = repository_relative_path(
            source, repository_root=repository_root
        )
        public_snapshots.append(public_path)
        target = staging / "repository" / public_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        document = _read_object(source)
        fetched_at = document.get("metadata", {}).get("fetched_at")
        if not isinstance(fetched_at, str):
            raise GuardedCollectionError(f"new raw snapshot lacks fetched_at: {public_path}")
        fetched = datetime.fromisoformat(fetched_at)
        log = raw_root.joinpath(
            "_query_log",
            fetched.strftime("%Y"),
            fetched.strftime("%m"),
            f"{fetched:%d}.jsonl",
        )
        if not log.is_file():
            raise GuardedCollectionError(f"new raw snapshot lacks query log: {public_path}")
        query_logs.add(log.resolve())

    public_logs = []
    for source in sorted(query_logs):
        public_path = repository_relative_path(
            source, repository_root=repository_root
        )
        public_logs.append(public_path)
        target = staging / "repository" / public_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return public_snapshots, public_logs


def write_artifact_bundle(
    audit: Dict[str, Any],
    *,
    artifact_root: Path,
    raw_root: Path,
    snapshot_paths: Sequence[Path],
    repository_root: Path,
    created_at: datetime,
) -> Path:
    artifact_root = repository_root / repository_relative_path(
        artifact_root, repository_root=repository_root
    )
    identity = "\0".join(
        (
            audit["preflight"]["policy_version"],
            audit["preflight"]["job_id"],
            audit["preflight"]["requested_action"],
            created_at.isoformat(timespec="microseconds"),
        )
    ).encode("utf-8")
    bundle_id = hashlib.sha256(identity).hexdigest()[:20]
    destination = artifact_root.joinpath(
        created_at.strftime("%Y"),
        created_at.strftime("%m"),
        created_at.strftime("%d"),
        bundle_id,
    )
    repository_relative_path(destination, repository_root=repository_root)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite collection artifact: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".collection-artifact-", dir=destination.parent))
    try:
        public_snapshots, public_logs = _copy_collection_evidence(
            staging,
            raw_root=raw_root,
            snapshot_paths=snapshot_paths,
            repository_root=repository_root,
        )
        audit["new_raw_snapshot_count"] = len(public_snapshots)
        audit["new_raw_snapshots"] = public_snapshots
        audit["query_logs"] = public_logs
        audit["raw_first_preserved"] = bool(public_snapshots) or (
            audit["preflight"]["requested_action"] == "preflight"
        )
        audit["credential_value_persisted"] = False
        (staging / "audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return destination


def run_guarded_collection(
    *,
    job_id: str,
    action: str,
    confirmation: str = "",
    plan_path: Path = DEFAULT_PLAN,
    policy_path: Path = DEFAULT_POLICY,
    raw_root: Path = DEFAULT_RAW_ROOT,
    artifact_root: Optional[Path] = None,
    repository_root: Path = REPOSITORY_ROOT,
    collector: Callable[..., Dict[str, Any]] = collect_job,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    plan_path = Path(plan_path)
    policy_path = Path(policy_path)
    raw_root = Path(raw_root)
    repository_relative_path(plan_path, repository_root=repository_root)
    repository_relative_path(policy_path, repository_root=repository_root)
    repository_relative_path(raw_root, repository_root=repository_root)
    plan = load_plan(plan_path)
    policy = load_policy(policy_path)
    if plan.get("source") != policy["source"]:
        raise GuardedCollectionError("collection plan and safety policy source differ")
    known_job_ids = {job["job_id"] for job in plan["jobs"]}
    unknown_allowed = sorted(set(policy["allowed_job_ids"]) - known_job_ids)
    if unknown_allowed:
        raise GuardedCollectionError(
            "safety policy contains unknown jobs: " + ", ".join(unknown_allowed)
        )
    credential_name = policy["credential_environment_variable"]
    credential_present = bool(os.environ.get(credential_name, ""))
    preflight = build_preflight(
        plan,
        policy,
        job_id=job_id,
        action=action,
        confirmation=confirmation,
        raw_root=raw_root,
        credential_present=credential_present,
    )
    before = _snapshot_paths(raw_root)
    collection_result = None
    runtime_error = None
    if action == "preflight":
        status = "preflight_completed"
    elif not preflight["collection_allowed"]:
        status = "blocked"
    else:
        try:
            collection_result = collector(
                plan,
                job_id,
                raw_root=raw_root,
                page_budget=preflight["planned_page_count"],
                timeout=policy["request_timeout_seconds"],
            )
            status = "succeeded"
        except Exception as error:  # Raw evidence is bundled before returning failure.
            runtime_error = error
            status = "failed"

    after = _snapshot_paths(raw_root)
    new_snapshots = sorted(after - before)
    jobs = {job["job_id"]: job for job in plan["jobs"]}
    status_after = inspect_job(jobs[job_id], raw_root=raw_root)
    public_result = None
    if collection_result is not None:
        public_result = {
            key: value
            for key, value in collection_result.items()
            if key not in {"query", "snapshot_paths"}
        }
    created = created_at or datetime.now(timezone.utc)
    audit = {
        "collection_audit_schema_version": 1,
        "created_at": created.isoformat(timespec="microseconds"),
        "status": status,
        "preflight": preflight,
        "collection_result": public_result,
        "job_status_after": _public_status(status_after),
        "runtime_error_type": type(runtime_error).__name__ if runtime_error else None,
        "workflow_context": {
            name: os.environ[name]
            for name in (
                "GITHUB_REPOSITORY",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "GITHUB_SHA",
                "GITHUB_REF",
            )
            if os.environ.get(name)
        },
    }
    destination = None
    if artifact_root is not None:
        destination = write_artifact_bundle(
            audit,
            artifact_root=Path(artifact_root),
            raw_root=raw_root,
            snapshot_paths=new_snapshots,
            repository_root=repository_root,
            created_at=created,
        )
    return {
        "audit": audit,
        "artifact_path": destination,
        "runtime_error": runtime_error,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preflight or run one manually authorized financial collection job."
    )
    parser.add_argument("--job", required=True)
    parser.add_argument("--action", choices=ACTIONS, default="preflight")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_guarded_collection(
            job_id=args.job,
            action=args.action,
            confirmation=args.confirmation,
            plan_path=args.plan,
            policy_path=args.policy,
            raw_root=args.raw_root,
            artifact_root=args.artifact_root,
        )
        printable = dict(result["audit"])
        if result["artifact_path"] is not None:
            printable["artifact_path"] = repository_relative_path(
                result["artifact_path"], repository_root=REPOSITORY_ROOT
            )
        print(json.dumps(printable, ensure_ascii=False, indent=2))
        if result["runtime_error"] is not None:
            print(f"error: {result['runtime_error']}", file=sys.stderr)
        return 0 if result["audit"]["status"] in {
            "preflight_completed",
            "succeeded",
        } else 1
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
