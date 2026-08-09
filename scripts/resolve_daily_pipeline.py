#!/usr/bin/env python3
"""Resolve local, complete inputs into deterministic daily pipeline steps."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.derive_advanced_financial_metrics import (  # noqa: E402
    CALCULATOR_VERSION as ADVANCED_CALCULATOR_VERSION,
)
from scripts.derive_financial_metrics import (  # noqa: E402
    CALCULATOR_VERSION as BASIC_CALCULATOR_VERSION,
)
from scripts.normalize_iwencai_financials import (  # noqa: E402
    NORMALIZER_VERSION as FINANCIAL_NORMALIZER_VERSION,
)
from scripts.repository_paths import (  # noqa: E402
    RepositoryPathError,
    repository_relative_path,
)
from scripts.resolve_research_pipeline import resolve_research_pipeline  # noqa: E402
from scripts.run_financial_collection_plan import inspect_job, load_plan  # noqa: E402


class ReadinessError(ValueError):
    """Raised when local inputs cannot be planned safely."""


def _read_object(path: Path) -> Dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ReadinessError(f"JSON root must be an object: {path}")
    return document


def _repository_path(value: Any, *, repository_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ReadinessError("readiness paths must be non-empty strings")
    try:
        relative = repository_relative_path(value, repository_root=repository_root)
    except RepositoryPathError as error:
        raise ReadinessError(str(error)) from error
    return repository_root / relative


def _string_list(value: Any, *, name: str) -> List[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ReadinessError(f"{name} must be a string array")
    if len(value) != len(set(value)):
        raise ReadinessError(f"{name} must not contain duplicates")
    return list(value)


def _financial_bundle_id(record_ids: Sequence[str], mapping_version: str) -> str:
    identity = "\0".join(
        [FINANCIAL_NORMALIZER_VERSION, mapping_version, *record_ids]
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:20]


def _complete_job_identity(
    status: Dict[str, Any],
    *,
    mapping_file: Path,
    normalized_root: Path,
    repository_root: Path,
) -> Dict[str, Any]:
    if status.get("status") != "complete" or status.get("errors"):
        raise ReadinessError("only complete, error-free jobs have an input identity")
    snapshots = [Path(path) for path in status.get("snapshot_paths", [])]
    if not snapshots:
        raise ReadinessError(f"complete job has no snapshots: {status['job_id']}")
    metadata = []
    for snapshot in snapshots:
        repository_relative_path(snapshot, repository_root=repository_root)
        document = _read_object(snapshot)
        item = document.get("metadata")
        if not isinstance(item, dict):
            raise ReadinessError(f"raw snapshot has no metadata: {snapshot}")
        record_id = item.get("record_id")
        fetched_at = item.get("fetched_at")
        if not isinstance(record_id, str) or not record_id:
            raise ReadinessError(f"raw snapshot has no record_id: {snapshot}")
        if not isinstance(fetched_at, str):
            raise ReadinessError(f"raw snapshot has no fetched_at: {snapshot}")
        metadata.append((record_id, datetime.fromisoformat(fetched_at)))

    mapping = _read_object(mapping_file)
    mapping_version = mapping.get("mapping_version")
    if not isinstance(mapping_version, str) or not mapping_version:
        raise ReadinessError("field mapping has no mapping_version")
    record_ids = [item[0] for item in metadata]
    fetched_start = min(item[1] for item in metadata)
    bundle_id = _financial_bundle_id(record_ids, mapping_version)
    manifest = normalized_root.joinpath(
        "runs",
        "iwencai",
        fetched_start.strftime("%Y"),
        fetched_start.strftime("%m"),
        fetched_start.strftime("%d"),
        bundle_id,
        "manifest.json",
    )
    manifest_relative = repository_relative_path(
        manifest, repository_root=repository_root
    )
    if manifest.exists():
        existing = _read_object(manifest)
        existing_ids = [
            item.get("record_id")
            for item in existing.get("raw_records", [])
            if isinstance(item, dict)
        ]
        if (
            existing.get("bundle_id") != bundle_id
            or existing.get("normalizer_version") != FINANCIAL_NORMALIZER_VERSION
            or existing.get("mapping_version") != mapping_version
            or existing_ids != record_ids
        ):
            raise ReadinessError(
                "normalized manifest does not match expected input identity: "
                + manifest_relative
            )
    return {
        "bundle_id": bundle_id,
        "manifest": manifest_relative,
        "manifest_exists": manifest.exists(),
    }


def _derived_manifests(derived_root: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    if not derived_root.exists():
        return []
    return [
        (path, _read_object(path))
        for path in sorted(derived_root.glob("runs/*/*/*/*/*/manifest.json"))
    ]


def _basic_metrics_exist(
    manifests: Sequence[Tuple[Path, Dict[str, Any]]],
    *,
    source_bundle_id: str,
    source_manifest: str,
    definition_version: str,
) -> Optional[Path]:
    for path, document in manifests:
        if (
            document.get("source_financial_bundle_id") == source_bundle_id
            and document.get("source_financial_manifest") == source_manifest
            and document.get("calculator_version") == BASIC_CALCULATOR_VERSION
            and document.get("metric_definition_version") == definition_version
            and document.get("table", {}).get("logical_name")
            == "financial_metrics"
        ):
            return path
    return None


def _advanced_metrics_exist(
    manifests: Sequence[Tuple[Path, Dict[str, Any]]],
    *,
    source_bundle_ids: Sequence[str],
    definition_version: str,
) -> Optional[Path]:
    expected = sorted(source_bundle_ids)
    for path, document in manifests:
        if (
            sorted(document.get("source_financial_bundle_ids", [])) == expected
            and document.get("calculator_version") == ADVANCED_CALCULATOR_VERSION
            and document.get("metric_definition_version") == definition_version
            and document.get("table", {}).get("logical_name")
            == "advanced_financial_metrics"
        ):
            return path
    return None


def _stage(document: Dict[str, Any], name: str) -> Dict[str, Any]:
    matches = [
        item
        for item in document.get("stages", [])
        if isinstance(item, dict) and item.get("stage") == name
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("steps"), list):
        raise ReadinessError(f"pipeline must contain one {name} stage")
    return matches[0]


def resolve_pipeline_config(
    document: Dict[str, Any],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    """Add work only for complete local inputs not already processed."""
    resolved = copy.deepcopy(document)
    settings = resolved.get("input_readiness")
    if settings is None:
        return resolved
    if not isinstance(settings, dict) or settings.get("schema_version") != 1:
        raise ReadinessError("input_readiness schema_version must be 1")

    plan_path = _repository_path(
        settings.get("financial_collection_plan"), repository_root=repository_root
    )
    raw_root = _repository_path(
        settings.get("raw_root"), repository_root=repository_root
    )
    mapping_file = _repository_path(
        settings.get("mapping_file"), repository_root=repository_root
    )
    normalized_root = _repository_path(
        settings.get("normalized_root"), repository_root=repository_root
    )
    derived_root = _repository_path(
        settings.get("derived_root"), repository_root=repository_root
    )
    basic_config = _repository_path(
        settings.get("basic_metric_config"), repository_root=repository_root
    )
    advanced_config = _repository_path(
        settings.get("advanced_metric_config"), repository_root=repository_root
    )
    normalizable_ids = _string_list(
        settings.get("normalization_job_ids"), name="normalization_job_ids"
    )
    basic_ids = _string_list(
        settings.get("basic_metric_job_ids"), name="basic_metric_job_ids"
    )
    advanced_ids = _string_list(
        settings.get("advanced_metric_job_ids"), name="advanced_metric_job_ids"
    )
    if not set(basic_ids + advanced_ids).issubset(set(normalizable_ids)):
        raise ReadinessError("metric jobs must also be normalization jobs")
    timeout = settings.get("timeout_seconds", 900)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ReadinessError("input_readiness timeout_seconds must be positive")

    plan = load_plan(plan_path)
    plan_relative = repository_relative_path(
        plan_path, repository_root=repository_root
    )
    raw_root_relative = repository_relative_path(
        raw_root, repository_root=repository_root
    )
    normalized_root_relative = repository_relative_path(
        normalized_root, repository_root=repository_root
    )
    derived_root_relative = repository_relative_path(
        derived_root, repository_root=repository_root
    )
    basic_config_relative = repository_relative_path(
        basic_config, repository_root=repository_root
    )
    advanced_config_relative = repository_relative_path(
        advanced_config, repository_root=repository_root
    )
    jobs_by_id = {job["job_id"]: job for job in plan["jobs"]}
    unknown = sorted(set(normalizable_ids + basic_ids + advanced_ids) - set(jobs_by_id))
    if unknown:
        raise ReadinessError(f"unknown readiness job ids: {', '.join(unknown)}")

    statuses = {
        job_id: inspect_job(jobs_by_id[job_id], raw_root=raw_root)
        for job_id in normalizable_ids
    }
    identities: Dict[str, Dict[str, Any]] = {}
    normalization_steps = []
    job_summaries = []
    for job_id in normalizable_ids:
        status = statuses[job_id]
        summary = {
            "job_id": job_id,
            "period_end": status["period_end"],
            "collection_status": status["status"],
            "collection_errors": status["errors"],
            "normalization_status": "waiting_for_complete_input",
            "normalized_manifest": None,
        }
        if status["status"] == "complete" and not status["errors"]:
            identity = _complete_job_identity(
                status,
                mapping_file=mapping_file,
                normalized_root=normalized_root,
                repository_root=repository_root,
            )
            identities[job_id] = identity
            summary["normalized_manifest"] = identity["manifest"]
            if identity["manifest_exists"]:
                summary["normalization_status"] = "up_to_date"
            else:
                summary["normalization_status"] = "planned"
                normalization_steps.append(
                    {
                        "step_id": f"normalize_financial_{job_id}",
                        "command": [
                            "scripts/run_financial_collection_plan.py",
                            "--plan",
                            plan_relative,
                            "--raw-root",
                            raw_root_relative,
                            "--normalized-root",
                            normalized_root_relative,
                            "normalize",
                            "--job",
                            job_id,
                        ],
                        "timeout_seconds": timeout,
                    }
                )
        job_summaries.append(summary)

    derived_manifests = _derived_manifests(derived_root)
    basic_definition = _read_object(basic_config).get("metric_definition_version")
    advanced_definition = _read_object(advanced_config).get(
        "metric_definition_version"
    )
    if not isinstance(basic_definition, str) or not basic_definition:
        raise ReadinessError("basic metric config has no definition version")
    if not isinstance(advanced_definition, str) or not advanced_definition:
        raise ReadinessError("advanced metric config has no definition version")

    derivation_steps = []
    basic_summaries = []
    for job_id in basic_ids:
        identity = identities.get(job_id)
        summary = {
            "job_id": job_id,
            "status": "waiting_for_normalized_input",
            "source_manifest": identity["manifest"] if identity else None,
            "derived_manifest": None,
        }
        if identity is not None:
            existing = _basic_metrics_exist(
                derived_manifests,
                source_bundle_id=identity["bundle_id"],
                source_manifest=identity["manifest"],
                definition_version=basic_definition,
            )
            if existing is not None:
                summary["status"] = "up_to_date"
                summary["derived_manifest"] = repository_relative_path(
                    existing, repository_root=repository_root
                )
            else:
                summary["status"] = "planned"
                derivation_steps.append(
                    {
                        "step_id": f"derive_basic_financial_{job_id}",
                        "command": [
                            "scripts/derive_financial_metrics.py",
                            identity["manifest"],
                            "--metric-config",
                            basic_config_relative,
                            "--derived-root",
                            derived_root_relative,
                        ],
                        "timeout_seconds": timeout,
                    }
                )
        basic_summaries.append(summary)

    missing_advanced = [job_id for job_id in advanced_ids if job_id not in identities]
    advanced_summary = {
        "job_ids": advanced_ids,
        "status": "waiting_for_normalized_inputs",
        "waiting_for": missing_advanced,
        "source_manifests": [
            identities[job_id]["manifest"]
            for job_id in advanced_ids
            if job_id in identities
        ],
        "derived_manifest": None,
    }
    if advanced_ids and not missing_advanced:
        source_ids = [identities[job_id]["bundle_id"] for job_id in advanced_ids]
        existing = _advanced_metrics_exist(
            derived_manifests,
            source_bundle_ids=source_ids,
            definition_version=advanced_definition,
        )
        if existing is not None:
            advanced_summary["status"] = "up_to_date"
            advanced_summary["derived_manifest"] = repository_relative_path(
                existing, repository_root=repository_root
            )
        else:
            advanced_summary["status"] = "planned"
            advanced_summary["waiting_for"] = []
            derivation_steps.append(
                {
                    "step_id": "derive_advanced_financial_metrics",
                    "command": [
                        "scripts/derive_advanced_financial_metrics.py",
                        *advanced_summary["source_manifests"],
                        "--config",
                        advanced_config_relative,
                        "--derived-root",
                        derived_root_relative,
                    ],
                    "timeout_seconds": timeout,
                }
            )
    elif not advanced_ids:
        advanced_summary["status"] = "not_configured"
        advanced_summary["waiting_for"] = []

    _stage(resolved, "normalization")["steps"].extend(normalization_steps)
    _stage(resolved, "derivation")["steps"].extend(derivation_steps)
    planned_count = len(normalization_steps) + len(derivation_steps)
    incomplete_count = sum(
        summary["collection_status"] != "complete" for summary in job_summaries
    )
    financial_status = (
        "work_planned"
        if planned_count
        else ("waiting_for_complete_input" if incomplete_count else "up_to_date")
    )
    resolved["readiness"] = {
        "schema_version": 1,
        "status": financial_status,
        "planned_step_count": planned_count,
        "incomplete_job_count": incomplete_count,
        "financial_jobs": job_summaries,
        "basic_financial_metrics": basic_summaries,
        "advanced_financial_metrics": advanced_summary,
    }

    research_settings = resolved.get("research_readiness")
    if research_settings is not None:
        research = resolve_research_pipeline(
            research_settings,
            repository_root=repository_root,
        )
        _stage(resolved, "normalization")["steps"].extend(
            research["normalization_steps"]
        )
        _stage(resolved, "derivation")["steps"].extend(
            research["derivation_steps"]
        )
        reporting_stage = _stage(resolved, "reporting")
        reporting_stage["steps"] = (
            research["reporting_steps"] + reporting_stage["steps"]
        )
        research_planned = research["planned_step_count"]
        combined_planned = planned_count + research_planned
        if combined_planned:
            combined_status = "work_planned"
        elif incomplete_count or research["status"] == "waiting_for_inputs":
            combined_status = "waiting_for_complete_input"
        else:
            combined_status = "up_to_date"
        resolved["readiness"]["status"] = combined_status
        resolved["readiness"]["planned_step_count"] = combined_planned
        resolved["readiness"]["research"] = {
            name: value
            for name, value in research.items()
            if not name.endswith("_steps")
        }
    return resolved


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve complete local inputs into a portable pipeline plan."
    )
    parser.add_argument(
        "--config", type=Path, default=REPOSITORY_ROOT / "config/daily_pipeline.json"
    )
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        document = _read_object(args.config)
        resolved = resolve_pipeline_config(document, repository_root=root)
        print(json.dumps(resolved, ensure_ascii=False, indent=2))
        return 0
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
