#!/usr/bin/env python3
"""Resolve daily monitoring tasks against the latest P0 research queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.investment_universe import (  # noqa: E402
    DEFAULT_UNIVERSE,
    load_investment_universe,
)
from scripts.repository_paths import repository_relative_path  # noqa: E402


DEFAULT_PLAN = REPOSITORY_ROOT / "config" / "codex_daily_collection.json"
DEFAULT_DERIVED_ROOT = REPOSITORY_ROOT / "data" / "derived"
MONITORING_KINDS = {"announcements", "news"}
SCOPE_SCHEMA_VERSION = 1


class MonitoringScopeError(ValueError):
    """Raised when a monitoring scope cannot be resolved deterministically."""


def _object(path: Path) -> Dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MonitoringScopeError(f"JSON root must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise MonitoringScopeError(
                    f"JSONL row must be an object: {path}:{line_number}"
                )
            yield value


def latest_p0_targets(
    *,
    derived_root: Path = DEFAULT_DERIVED_ROOT,
    universe_path: Path = DEFAULT_UNIVERSE,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    """Return the latest complete P0 target list with manifest lineage."""
    universe = load_investment_universe(universe_path)
    candidates = []
    for manifest_path in Path(derived_root).glob("runs/screening/**/manifest.json"):
        manifest = _object(manifest_path)
        table = manifest.get("table")
        if (
            manifest.get("universe_id") != universe["stocks"]["universe_id"]
            or not isinstance(table, dict)
            or table.get("logical_name") != "market_research_queue"
            or not isinstance(table.get("file"), str)
        ):
            continue
        records_path = manifest_path.parent / table["file"]
        if not records_path.is_file() or _sha256(records_path) != table.get("sha256"):
            raise MonitoringScopeError(
                f"screening table hash mismatch: {manifest_path}"
            )
        records = list(_jsonl(records_path))
        observed_dates = {
            item.get("as_of_date")
            for item in records
            if isinstance(item.get("as_of_date"), str)
        }
        if not observed_dates:
            continue
        targets = [
            {
                "security_code": item["security_code"],
                "security_name": item["security_name"],
            }
            for item in records
            if item.get("priority") == "P0"
            and isinstance(item.get("security_code"), str)
            and isinstance(item.get("security_name"), str)
            and item["security_name"]
        ]
        targets.sort(key=lambda item: item["security_code"])
        if len({item["security_code"] for item in targets}) != len(targets):
            raise MonitoringScopeError(
                f"P0 target codes are duplicated: {manifest_path}"
            )
        candidates.append(
            {
                "as_of_date": max(observed_dates),
                "manifest": manifest_path,
                "targets": targets,
            }
        )
    if not candidates:
        raise MonitoringScopeError("no configured-universe screening bundle is available")
    latest = max(
        candidates,
        key=lambda item: (item["as_of_date"], item["manifest"].as_posix()),
    )
    if not latest["targets"]:
        raise MonitoringScopeError("latest screening bundle contains no P0 targets")
    return {
        "priority": "P0",
        "as_of_date": latest["as_of_date"],
        "source_manifest": repository_relative_path(
            latest["manifest"], repository_root=repository_root
        ),
        "targets": latest["targets"],
    }


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MonitoringScopeError(f"{label} must be a positive integer")
    return value


def _chunks(items: Sequence[Dict[str, str]], size: int) -> Iterable[List[Dict[str, str]]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def resolve_monitoring_jobs(
    *,
    plan_path: Path = DEFAULT_PLAN,
    derived_root: Path = DEFAULT_DERIVED_ROOT,
    universe_path: Path = DEFAULT_UNIVERSE,
    repository_root: Path = REPOSITORY_ROOT,
    run_date: Optional[str] = None,
) -> Dict[str, Any]:
    plan = _object(plan_path)
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        raise MonitoringScopeError("daily collection tasks must be an array")
    run_date = date.fromisoformat(run_date or date.today().isoformat()).isoformat()
    p0 = None
    jobs = []
    for task in tasks:
        if not isinstance(task, dict) or task.get("dataset_kind") not in MONITORING_KINDS:
            continue
        task_id = task.get("task_id")
        template = task.get("query_template")
        scope_type = task.get("scope_type")
        if not all(isinstance(value, str) and value for value in (task_id, template, scope_type)):
            raise MonitoringScopeError("monitoring task identity, query, and scope are required")
        base = {
            "task_id": task_id,
            "dataset_kind": task["dataset_kind"],
            "tool": task.get("tool"),
            "as_of_date": run_date,
            "requested_result_count": _positive_int(
                task.get("requested_result_count", 10), "requested_result_count"
            ),
        }
        if scope_type == "market_wide":
            jobs.append(
                {
                    **base,
                    "collection_id": f"{task_id}-{run_date}",
                    "query": template,
                    "collection_scope": {
                        "scope_schema_version": SCOPE_SCHEMA_VERSION,
                        "scope_type": "market_wide",
                        "topic_id": task_id,
                    },
                }
            )
            continue
        if scope_type != "latest_p0":
            raise MonitoringScopeError(f"unsupported monitoring scope: {scope_type}")
        if p0 is None:
            p0 = latest_p0_targets(
                derived_root=derived_root,
                universe_path=universe_path,
                repository_root=repository_root,
            )
        maximum = _positive_int(task.get("maximum_target_count"), "maximum_target_count")
        batch_size = _positive_int(task.get("target_batch_size"), "target_batch_size")
        if len(p0["targets"]) > maximum:
            raise MonitoringScopeError(
                f"P0 target count {len(p0['targets'])} exceeds configured maximum {maximum}"
            )
        for batch_number, target_batch in enumerate(
            _chunks(p0["targets"], batch_size), start=1
        ):
            target_text = "、".join(
                f"{item['security_code']} {item['security_name']}"
                for item in target_batch
            )
            scope = {
                "scope_schema_version": SCOPE_SCHEMA_VERSION,
                "scope_type": "p0_securities",
                "priority": "P0",
                "target_source_manifest": p0["source_manifest"],
                "target_as_of_date": p0["as_of_date"],
                "target_security_codes": [
                    item["security_code"] for item in target_batch
                ],
            }
            allowed = task.get("allowed_event_types")
            if allowed is not None:
                if (
                    not isinstance(allowed, list)
                    or not allowed
                    or any(not isinstance(item, str) or not item for item in allowed)
                ):
                    raise MonitoringScopeError("allowed_event_types must contain strings")
                scope["allowed_event_types"] = allowed
            jobs.append(
                {
                    **base,
                    "collection_id": f"{task_id}-{run_date}-batch{batch_number:02d}",
                    "query": template.format(p0_security_list=target_text),
                    "collection_scope": scope,
                }
            )
    return {
        "scope_resolution_version": "1.0.0",
        "run_date": run_date,
        "p0_target_count": len(p0["targets"]) if p0 else 0,
        "p0_source_manifest": p0["source_manifest"] if p0 else None,
        "job_count": len(jobs),
        "jobs": jobs,
        "investment_judgment_included": False,
        "automatic_trading_enabled": False,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="按最新 P0 队列生成公司监控查询，并保留宏观固定查询。"
    )
    parser.add_argument("--date", dest="run_date")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--derived-root", type=Path, default=DEFAULT_DERIVED_ROOT)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    try:
        result = resolve_monitoring_jobs(
            plan_path=args.plan,
            derived_root=args.derived_root,
            universe_path=args.universe,
            repository_root=args.root,
            run_date=args.run_date,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
