#!/usr/bin/env python3
"""Validate repository data lineage, manifests, query logs, and file limits."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.repository_paths import repository_relative_path  # noqa: E402
from scripts.investment_universe import (  # noqa: E402
    InvestmentUniverseError,
    load_investment_universe,
)


GITHUB_FILE_LIMIT = 100 * 1024 * 1024
PUBLIC_ARTIFACT_SUFFIXES = {
    ".csv",
    ".json",
    ".jsonl",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
}
LOCAL_PATH_PATTERNS = (
    ("macOS user path", re.compile(rb"(?<![A-Za-z0-9])/Users/")),
    ("Linux home path", re.compile(rb"(?<![A-Za-z0-9])/home/")),
    (
        "Windows user path",
        re.compile(
            rb"(?<![A-Za-z0-9])[A-Za-z]:[\\/]+Users[\\/]",
            re.IGNORECASE,
        ),
    ),
)


def _label(root: Path, path: Path) -> str:
    return repository_relative_path(path, repository_root=root)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_snapshots(root: Path) -> Iterable[Path]:
    raw_root = root / "data" / "raw"
    for source in raw_root.iterdir():
        if source.is_dir() and source.name != "_query_log":
            yield from source.glob("*/*/*/*.json")


def _validate_raw(root: Path, errors: List[str]) -> Dict[str, Dict[str, Any]]:
    metadata_by_relative = {}
    raw_root = root / "data" / "raw"
    for path in _raw_snapshots(root):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            metadata, payload = document["metadata"], document["payload"]
            digest = hashlib.sha256(_canonical(payload)).hexdigest()
            if digest != metadata.get("payload_sha256"):
                errors.append(f"{_label(root, path)}: payload hash mismatch")
            relative = path.relative_to(raw_root).as_posix()
            metadata_by_relative[relative] = metadata
        except (OSError, KeyError, TypeError, ValueError) as error:
            errors.append(f"{_label(root, path)}: invalid raw envelope ({error})")
    return metadata_by_relative


def _validate_query_logs(
    root: Path,
    raw_metadata: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> None:
    log_root = root / "data" / "raw" / "_query_log"
    logged = set()
    for path in log_root.glob("*/*/*.jsonl"):
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    entry = json.loads(line)
                    relative = entry["raw_relative_path"]
                except (json.JSONDecodeError, KeyError, TypeError) as error:
                    errors.append(
                        f"{_label(root, path)}:{line_number}: "
                        f"invalid query-log entry ({error})"
                    )
                    continue
                if relative in logged:
                    errors.append(
                        f"{_label(root, path)}:{line_number}: "
                        "duplicate raw_relative_path"
                    )
                logged.add(relative)
                metadata = raw_metadata.get(relative)
                if metadata is None:
                    errors.append(
                        f"{_label(root, path)}:{line_number}: "
                        "raw snapshot does not exist"
                    )
                elif entry.get("record_id") != metadata.get("record_id"):
                    errors.append(
                        f"{_label(root, path)}:{line_number}: record_id mismatch"
                    )
    missing = sorted(set(raw_metadata) - logged)
    for relative in missing:
        errors.append(f"{relative}: missing query-log entry")


def _table_files(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    tables = manifest.get("tables")
    if isinstance(tables, dict):
        for table in tables.values():
            if not isinstance(table, dict):
                continue
            partitions = table.get("partitions")
            if isinstance(partitions, list):
                entries.extend(partitions)
            elif isinstance(table.get("file"), str):
                entries.append(table)
    table = manifest.get("table")
    if isinstance(table, dict):
        partitions = table.get("partitions")
        if isinstance(partitions, list):
            entries.extend(partitions)
        elif isinstance(table.get("file"), str):
            entries.append(table)
    return entries


def _validate_manifests(root: Path, errors: List[str]) -> None:
    for area in ("normalized", "derived"):
        for path in (root / "data" / area).glob("runs/**/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                entries = _table_files(manifest)
                if not entries:
                    errors.append(
                        f"{_label(root, path)}: manifest has no physical table files"
                    )
                for entry in entries:
                    target = path.parent / entry["file"]
                    if not target.is_file():
                        errors.append(
                            f"{_label(root, path)}: missing table file {entry['file']}"
                        )
                    elif _sha(target) != entry.get("sha256"):
                        errors.append(
                            f"{_label(root, path)}: hash mismatch for {entry['file']}"
                        )
            except (OSError, KeyError, TypeError, ValueError) as error:
                errors.append(
                    f"{_label(root, path)}: invalid manifest ({error})"
                )


def _validate_json_documents(root: Path, errors: List[str]) -> None:
    paths = list((root / "config").glob("*.json"))
    paths.extend((root / "portfolio").glob("*.template.json"))
    paths.extend((root / "research_queue").glob("*.template.json"))
    paths.extend((root / "decision_journal").glob("*.template.json"))
    for path in paths:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"{_label(root, path)}: invalid JSON ({error})")


def _validate_collection_scope(root: Path, errors: List[str]) -> None:
    universe_path = root / "config/investment_universe.json"
    budget_path = root / "config/collection_budget.json"
    daily_path = root / "config/codex_daily_collection.json"
    requirements_path = root / "config/system_completion_requirements.json"
    if not any(path.exists() for path in (universe_path, budget_path)):
        return
    if not all(path.is_file() for path in (universe_path, budget_path, daily_path)):
        errors.append("collection scope configuration is incomplete")
        return
    try:
        universe = load_investment_universe(universe_path)
        budget = json.loads(budget_path.read_text(encoding="utf-8"))
        daily = json.loads(daily_path.read_text(encoding="utf-8"))
        if budget.get("schema_version") != 1:
            raise ValueError("collection budget schema_version must be 1")
        observed = budget.get("daily_observed_limit")
        safe = budget.get("daily_safe_limit")
        reserve = budget.get("reserved_requests")
        page_size = budget.get("page_size")
        if any(
            not isinstance(value, int) or value < 0
            for value in (observed, safe, reserve, page_size)
        ) or page_size == 0:
            raise ValueError("collection budget limits must be non-negative integers")
        if safe + reserve > observed:
            raise ValueError("safe limit plus reserve exceeds observed daily limit")
        for day_type in ("trading_day", "non_trading_day"):
            allocations = budget.get(day_type)
            if not isinstance(allocations, dict) or any(
                not isinstance(value, int) or value < 0
                for value in allocations.values()
            ):
                raise ValueError(f"{day_type} allocations must be non-negative integers")
            if sum(allocations.values()) > safe:
                raise ValueError(f"{day_type} allocations exceed daily safe limit")
        if daily.get("investment_universe") != "config/investment_universe.json":
            raise ValueError("daily plan must reference the investment-universe config")
        if daily.get("collection_budget") != "config/collection_budget.json":
            raise ValueError("daily plan must reference the collection-budget config")
        if daily.get("monitoring_scope_resolver") != (
            "scripts/resolve_monitoring_collection_scope.py"
        ):
            raise ValueError("daily plan must reference the monitoring scope resolver")
        tasks = daily.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("daily collection tasks must be an array")
        by_kind = {
            task.get("dataset_kind"): task
            for task in tasks
            if isinstance(task, dict)
        }
        if by_kind.get("etf", {}).get("tool") != "hithink-etf-selector":
            raise ValueError("daily plan is missing the dedicated ETF collection task")
        if by_kind.get("market", {}).get("complete_pagination_required") is not True:
            raise ValueError("stock-market collection must require complete pagination")
        by_id = {
            task.get("task_id"): task
            for task in tasks
            if isinstance(task, dict)
        }
        for task_id in ("p0_repurchase_announcements", "p0_company_news"):
            task = by_id.get(task_id, {})
            if (
                task.get("scope_type") != "latest_p0"
                or task.get("normalized_results_must_match_scope") is not True
            ):
                raise ValueError(f"{task_id} must be limited to the latest P0 scope")
        if by_id.get("p0_repurchase_announcements", {}).get("allowed_event_types") != [
            "share_repurchase"
        ]:
            raise ValueError("P0 announcement monitoring must be limited to repurchases")
        for task_id in (
            "china_macro_policy_news",
            "global_macro_market_news",
            "industry_policy_news",
        ):
            if by_id.get(task_id, {}).get("scope_type") != "market_wide":
                raise ValueError(f"{task_id} must retain market-wide scope")
        if requirements_path.is_file():
            requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
            minimum = requirements.get("minimum_counts", {})
            if minimum.get("full_market_securities") != universe["stocks"][
                "minimum_expected_count"
            ]:
                raise ValueError("market audit threshold disagrees with stock universe")
    except (InvestmentUniverseError, OSError, TypeError, ValueError) as error:
        errors.append(f"collection scope configuration is invalid ({error})")


def _validate_file_sizes(root: Path, errors: List[str]) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "private" in path.parts:
            continue
        if path.stat().st_size >= GITHUB_FILE_LIMIT:
            errors.append(
                f"{_label(root, path)}: exceeds GitHub 100 MiB file limit"
            )


def _git_tracked_paths(root: Path) -> List[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return []
    names = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return [Path(name) for name in names if name]


def _is_public_artifact(path: Path) -> bool:
    if path.suffix.lower() not in PUBLIC_ARTIFACT_SUFFIXES:
        return False
    parts = path.parts
    if parts[:2] in (("data", "normalized"), ("data", "derived")):
        return True
    if parts and parts[0] in {
        "config",
        "decision_journal",
        "portfolio",
        "reports",
        "research_queue",
    }:
        return True
    return False


def _validate_tracked_privacy(
    root: Path,
    tracked_paths: Sequence[Path],
    errors: List[str],
) -> None:
    for supplied_path in tracked_paths:
        path = Path(supplied_path)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(root.resolve())
            except ValueError:
                errors.append("tracked path is outside repository root")
                continue
        public_name = path.as_posix()
        if "private" in path.parts or fnmatch.fnmatch(path.name, "*.private.*"):
            errors.append(f"{public_name}: private file must not be tracked")
            continue
        if not _is_public_artifact(path):
            continue
        artifact = root / path
        if not artifact.is_file():
            continue
        try:
            content = artifact.read_bytes()
        except OSError as error:
            errors.append(f"{public_name}: cannot inspect public artifact ({error})")
            continue
        for description, pattern in LOCAL_PATH_PATTERNS:
            if pattern.search(content):
                errors.append(
                    f"{public_name}: contains machine-local absolute path "
                    f"({description})"
                )


def validate_repository(
    root: Path = REPOSITORY_ROOT,
    *,
    tracked_paths: Optional[Sequence[Path]] = None,
) -> List[str]:
    root = Path(root)
    errors: List[str] = []
    raw_metadata = _validate_raw(root, errors)
    _validate_query_logs(root, raw_metadata, errors)
    _validate_manifests(root, errors)
    _validate_json_documents(root, errors)
    _validate_collection_scope(root, errors)
    _validate_file_sizes(root, errors)
    if tracked_paths is None:
        tracked_paths = _git_tracked_paths(root)
    _validate_tracked_privacy(root, tracked_paths, errors)
    return errors


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate repository integrity.")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    errors = validate_repository(args.root)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("repository integrity validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
