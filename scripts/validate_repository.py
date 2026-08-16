#!/usr/bin/env python3
"""Validate repository data lineage, manifests, query logs, and file limits."""

from __future__ import annotations

import argparse
import csv
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
from scripts.public_payload_safety import (  # noqa: E402
    PublicPayloadSafetyError,
    assert_public_payload_safe,
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
STRUCTURED_PUBLIC_PAYLOAD_SUFFIXES = {".csv", ".json", ".jsonl"}
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


def _validate_github_connector_exports(root: Path, errors: List[str]) -> None:
    pattern = "runs/screening/**/github_connector/manifest.json"
    for path in (root / "data" / "derived").glob(pattern):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest.get("kind") != "github_connector_export":
                raise ValueError("unexpected connector manifest kind")
            limit = manifest["max_file_size_bytes"]
            if not isinstance(limit, int) or limit <= 0 or limit >= 1024 * 1024:
                raise ValueError("connector file limit must be between 1 byte and 1 MiB")

            source_manifest_path = (path.parent / manifest["source_manifest"]).resolve()
            source_manifest_path.relative_to(root.resolve())
            source_manifest = json.loads(
                source_manifest_path.read_text(encoding="utf-8")
            )
            source_table = source_manifest["table"]
            declared_source = manifest["source_table"]
            for field in ("file", "record_count", "sha256"):
                if declared_source.get(field) != source_table.get(field):
                    raise ValueError(f"source table {field} mismatch")

            tables = manifest["tables"]
            summary = tables["p0_p1_summary"]
            summary_path = path.parent / summary["file"]
            if summary_path.stat().st_size != summary.get("byte_size"):
                raise ValueError("P0/P1 summary byte size mismatch")
            if summary_path.stat().st_size > limit:
                raise ValueError("P0/P1 summary exceeds connector file limit")
            observed_priorities = {"P0": 0, "P1": 0}
            observed_summary_count = 0
            with summary_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    priority = json.loads(line).get("priority")
                    if priority not in observed_priorities:
                        raise ValueError("P0/P1 summary contains another priority")
                    observed_priorities[priority] += 1
                    observed_summary_count += 1
            if observed_summary_count != summary.get("record_count"):
                raise ValueError("P0/P1 summary record count mismatch")
            if observed_priorities != summary.get("priority_counts"):
                raise ValueError("P0/P1 summary priority counts mismatch")

            digest = hashlib.sha256()
            observed_full_count = 0
            for partition in tables["full_queue"]["partitions"]:
                partition_path = path.parent / partition["file"]
                content = partition_path.read_bytes()
                if len(content) != partition.get("byte_size"):
                    raise ValueError(f"partition byte size mismatch: {partition['file']}")
                if len(content) > limit:
                    raise ValueError(f"partition exceeds connector limit: {partition['file']}")
                digest.update(content)
                observed_full_count += partition["record_count"]
            if observed_full_count != tables["full_queue"].get("record_count"):
                raise ValueError("connector partition record count mismatch")
            if observed_full_count != source_table.get("record_count"):
                raise ValueError("connector record count differs from source table")
            if digest.hexdigest() != source_table.get("sha256"):
                raise ValueError("connector partitions do not reconstruct source queue")
        except (OSError, KeyError, TypeError, ValueError) as error:
            errors.append(
                f"{_label(root, path)}: invalid GitHub connector export ({error})"
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


def _git_publish_candidate_paths(root: Path, errors: List[str]) -> List[Path]:
    """Return tracked and non-ignored untracked files Git may publish."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        errors.append("cannot enumerate Git publish candidates")
        return []
    if result.returncode != 0:
        errors.append("cannot enumerate Git publish candidates")
        return []
    names = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return [Path(name) for name in names if name]


def _validate_file_sizes(
    root: Path,
    publish_candidate_paths: Sequence[Path],
    errors: List[str],
) -> None:
    for relative_path in publish_candidate_paths:
        path = root / relative_path
        try:
            if not path.is_file() or path.stat().st_size < GITHUB_FILE_LIMIT:
                continue
        except OSError as error:
            errors.append(
                f"{relative_path.as_posix()}: cannot inspect file size ({error})"
            )
            continue
        errors.append(
            f"{relative_path.as_posix()}: exceeds GitHub 100 MiB file limit"
        )


def _validate_publish_candidate_payload_safety(
    root: Path,
    publish_candidate_paths: Sequence[Path],
    errors: List[str],
) -> None:
    """Apply the shared privacy rule to every structured Git candidate."""
    for relative_path in publish_candidate_paths:
        if relative_path.suffix.lower() not in STRUCTURED_PUBLIC_PAYLOAD_SUFFIXES:
            continue
        path = root / relative_path
        if not path.is_file():
            continue
        public_name = relative_path.as_posix()
        try:
            if relative_path.suffix.lower() == ".json":
                with path.open("r", encoding="utf-8") as handle:
                    assert_public_payload_safe(json.load(handle))
            elif relative_path.suffix.lower() == ".jsonl":
                with path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        try:
                            assert_public_payload_safe(json.loads(line))
                        except PublicPayloadSafetyError as error:
                            errors.append(
                                f"{public_name}:{line_number}: {error}"
                            )
                            break
            else:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    if reader.fieldnames is not None:
                        assert_public_payload_safe(
                            {field_name: None for field_name in reader.fieldnames}
                        )
                    for row in reader:
                        assert_public_payload_safe(row)
        except PublicPayloadSafetyError as error:
            errors.append(f"{public_name}: {error}")
        except (OSError, UnicodeError, csv.Error, json.JSONDecodeError) as error:
            errors.append(
                f"{public_name}: cannot inspect structured public payload "
                f"({type(error).__name__})"
            )


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
    _validate_github_connector_exports(root, errors)
    _validate_json_documents(root, errors)
    _validate_collection_scope(root, errors)
    publish_candidate_paths = _git_publish_candidate_paths(root, errors)
    _validate_file_sizes(root, publish_candidate_paths, errors)
    _validate_publish_candidate_payload_safety(
        root, publish_candidate_paths, errors
    )
    if tracked_paths is None:
        tracked_paths = publish_candidate_paths
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
