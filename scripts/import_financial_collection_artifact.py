#!/usr/bin/env python3
"""Validate and idempotently import a guarded collection artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.repository_paths import repository_relative_path  # noqa: E402
from scripts.save_raw_response import PROJECT_TIMEZONE, _append_query_log  # noqa: E402


DEFAULT_REPORTS_ROOT = REPOSITORY_ROOT / "reports" / "daily" / "collection-imports"
LOCAL_PATH_PATTERNS = (
    re.compile(rb"(?<![A-Za-z0-9])/Users/"),
    re.compile(rb"(?<![A-Za-z0-9])/home/"),
    re.compile(rb"(?<![A-Za-z0-9])[A-Za-z]:[\\/]+Users[\\/]", re.IGNORECASE),
)
WORKFLOW_CONTEXT_KEYS = {
    "GITHUB_REPOSITORY",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_SHA",
    "GITHUB_REF",
}


class ArtifactImportError(ValueError):
    """Raised when a collection artifact cannot be trusted or imported."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact_bundle(path: Path) -> Tuple[Path, Path]:
    supplied = Path(path)
    if supplied.is_file() and supplied.name == "audit.json":
        audit_path = supplied
    else:
        direct = supplied / "audit.json"
        if direct.is_file():
            audit_path = direct
        else:
            matches = sorted(supplied.rglob("audit.json")) if supplied.is_dir() else []
            if len(matches) != 1:
                raise ArtifactImportError(
                    "artifact path must contain exactly one collection audit.json"
                )
            audit_path = matches[0]
    if audit_path.is_symlink():
        raise ArtifactImportError("collection audit.json must not be a symlink")
    return audit_path.parent, audit_path


def _public_repository_path(
    value: Any,
    *,
    repository_root: Path,
    prefix: str,
) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactImportError("artifact repository paths must be non-empty strings")
    relative = repository_relative_path(value, repository_root=repository_root)
    if relative != prefix and not relative.startswith(prefix + "/"):
        raise ArtifactImportError(f"artifact path is outside {prefix}: {relative}")
    return relative


def _artifact_file(bundle: Path, public_path: str) -> Path:
    supplied_tree = bundle / "repository"
    if supplied_tree.is_symlink():
        raise ArtifactImportError("artifact repository tree must not be a symlink")
    tree = supplied_tree.resolve()
    source = tree / public_path
    resolved = source.resolve()
    try:
        resolved.relative_to(tree)
    except ValueError as error:
        raise ArtifactImportError("artifact repository path escapes its bundle") from error
    if source.is_symlink() or not source.is_file():
        raise ArtifactImportError(f"artifact file is missing or unsafe: {public_path}")
    return source


def _validate_raw_snapshot(
    path: Path,
    *,
    public_path: str,
) -> Tuple[bytes, Dict[str, Any], str]:
    content = path.read_bytes()
    document = json.loads(content.decode("utf-8"))
    if not isinstance(document, dict):
        raise ArtifactImportError(f"raw snapshot root is invalid: {public_path}")
    metadata = document.get("metadata")
    payload = document.get("payload")
    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        raise ArtifactImportError(f"raw envelope is invalid: {public_path}")
    digest = _sha256(_canonical(payload))
    if digest != metadata.get("payload_sha256"):
        raise ArtifactImportError(f"raw payload hash mismatch: {public_path}")
    required = ("source", "query", "fetched_at", "record_id", "payload_sha256")
    if any(not isinstance(metadata.get(name), str) or not metadata[name] for name in required):
        raise ArtifactImportError(f"raw metadata is incomplete: {public_path}")
    if metadata["source"] != "iwencai":
        raise ArtifactImportError(f"unexpected raw source: {public_path}")
    if metadata.get("schema_version") != 1:
        raise ArtifactImportError(f"unsupported raw schema: {public_path}")
    try:
        fetched = datetime.fromisoformat(metadata["fetched_at"])
    except ValueError as error:
        raise ArtifactImportError(f"invalid Raw fetched_at: {public_path}") from error
    if fetched.tzinfo is None or fetched.utcoffset() is None:
        raise ArtifactImportError(f"Raw fetched_at has no timezone: {public_path}")
    fetched = fetched.astimezone(PROJECT_TIMEZONE)
    expected_prefix = fetched.strftime("%Y%m%dT%H%M%S%f%z") + "_"
    if not Path(public_path).name.startswith(expected_prefix):
        raise ArtifactImportError(f"raw filename timestamp mismatch: {public_path}")
    if not Path(public_path).name.endswith("_" + metadata["record_id"] + ".json"):
        raise ArtifactImportError(f"raw filename record_id mismatch: {public_path}")
    parts = Path(public_path).parts
    if parts[3:6] != (
        fetched.strftime("%Y"),
        fetched.strftime("%m"),
        fetched.strftime("%d"),
    ):
        raise ArtifactImportError(f"raw partition date mismatch: {public_path}")
    raw_relative = Path(public_path).relative_to("data/raw").as_posix()
    return content, metadata, raw_relative


def _query_log_entries(path: Path, *, public_path: str) -> List[Dict[str, Any]]:
    entries = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise ArtifactImportError(
                    f"invalid query log {public_path}:{line_number}"
                ) from error
            if not isinstance(entry, dict):
                raise ArtifactImportError(
                    f"invalid query log object {public_path}:{line_number}"
                )
            entries.append(entry)
    return entries


def validate_artifact(
    artifact_path: Path,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    bundle, audit_path = _artifact_bundle(Path(artifact_path))
    audit_content = audit_path.read_bytes()
    if any(pattern.search(audit_content) for pattern in LOCAL_PATH_PATTERNS):
        raise ArtifactImportError("collection audit contains a machine-local path")
    audit = json.loads(audit_content.decode("utf-8"))
    if not isinstance(audit, dict) or audit.get("collection_audit_schema_version") != 1:
        raise ArtifactImportError("unsupported collection audit schema")
    if audit.get("status") not in {"succeeded", "failed"}:
        raise ArtifactImportError("only collection attempts with Raw evidence may import")
    if audit.get("raw_first_preserved") is not True:
        raise ArtifactImportError("collection audit does not prove Raw-first preservation")
    if audit.get("credential_value_persisted") is not False:
        raise ArtifactImportError("collection audit does not prove credential exclusion")
    created_at = audit.get("created_at")
    if not isinstance(created_at, str):
        raise ArtifactImportError("collection created_at is missing")
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError as error:
        raise ArtifactImportError("collection created_at is invalid") from error
    if created.tzinfo is None or created.utcoffset() is None:
        raise ArtifactImportError("collection created_at must include a timezone")
    workflow_context = audit.get("workflow_context", {})
    if not isinstance(workflow_context, dict):
        raise ArtifactImportError("collection workflow_context must be an object")
    if not set(workflow_context).issubset(WORKFLOW_CONTEXT_KEYS):
        raise ArtifactImportError("collection workflow_context contains unexpected fields")
    if any(not isinstance(value, str) or not value for value in workflow_context.values()):
        raise ArtifactImportError("collection workflow_context values must be strings")
    preflight = audit.get("preflight")
    if not isinstance(preflight, dict) or preflight.get("requested_action") != "collect":
        raise ArtifactImportError("artifact was not produced by a collection action")
    if any(
        not isinstance(preflight.get(name), str) or not preflight[name]
        for name in ("job_id", "policy_version")
    ):
        raise ArtifactImportError("collection preflight identity is incomplete")
    raw_paths = audit.get("new_raw_snapshots")
    log_paths = audit.get("query_logs")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ArtifactImportError("collection artifact contains no Raw snapshots")
    if audit.get("new_raw_snapshot_count") != len(raw_paths):
        raise ArtifactImportError("collection audit Raw snapshot count mismatch")
    if not isinstance(log_paths, list) or not log_paths:
        raise ArtifactImportError("collection artifact contains no query logs")
    public_logs = [
        _public_repository_path(
            value,
            repository_root=repository_root,
            prefix="data/raw/_query_log",
        )
        for value in log_paths
    ]
    if len(public_logs) != len(set(public_logs)):
        raise ArtifactImportError("collection artifact contains duplicate query-log paths")
    log_entries: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for public_log in public_logs:
        source = _artifact_file(bundle, public_log)
        for entry in _query_log_entries(source, public_path=public_log):
            relative = entry.get("raw_relative_path")
            if not isinstance(relative, str) or not relative:
                raise ArtifactImportError(f"query log entry has no path: {public_log}")
            if relative in log_entries:
                raise ArtifactImportError(f"duplicate query log entry: {relative}")
            log_entries[relative] = (public_log, entry)

    public_snapshots = [
        _public_repository_path(
            value,
            repository_root=repository_root,
            prefix="data/raw/iwencai",
        )
        for value in raw_paths
    ]
    if len(public_snapshots) != len(set(public_snapshots)):
        raise ArtifactImportError("collection artifact contains duplicate Raw paths")

    snapshots = []
    for public_path in public_snapshots:
        source = _artifact_file(bundle, public_path)
        content, metadata, raw_relative = _validate_raw_snapshot(
            source, public_path=public_path
        )
        matching = log_entries.get(raw_relative)
        if matching is None:
            raise ArtifactImportError(f"Raw snapshot has no query log entry: {public_path}")
        public_log, entry = matching
        fetched = datetime.fromisoformat(metadata["fetched_at"]).astimezone(
            PROJECT_TIMEZONE
        )
        expected_log = (
            "data/raw/_query_log/"
            + fetched.strftime("%Y/%m/%d")
            + ".jsonl"
        )
        if public_log != expected_log:
            raise ArtifactImportError(
                f"Raw snapshot is linked to the wrong query log: {public_path}"
            )
        for name in (
            "source",
            "query",
            "fetched_at",
            "record_id",
            "schema_version",
            "payload_sha256",
        ):
            if entry.get(name) != metadata.get(name):
                raise ArtifactImportError(
                    f"Raw/query-log metadata mismatch for {name}: {public_path}"
                )
        snapshots.append(
            {
                "public_path": public_path,
                "content": content,
                "metadata": metadata,
                "raw_relative_path": raw_relative,
                "query_log_path": public_log,
                "query_log_entry": entry,
            }
        )
    return {
        "bundle": bundle,
        "audit": audit,
        "audit_sha256": _sha256(audit_content),
        "created": created,
        "snapshots": snapshots,
        "query_logs": public_logs,
    }


def _write_exclusive_or_verify(path: Path, content: bytes) -> bool:
    if path.exists():
        if path.read_bytes() != content:
            raise ArtifactImportError(f"refusing to overwrite different file: {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != content:
            raise ArtifactImportError(f"concurrent file conflict: {path}")
        return False
    return True


def _merge_query_logs(
    validated: Dict[str, Any],
    *,
    repository_root: Path,
) -> None:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for snapshot in validated["snapshots"]:
        grouped.setdefault(snapshot["query_log_path"], []).append(
            snapshot["query_log_entry"]
        )
    for public_path, entries in sorted(grouped.items()):
        destination = repository_root / public_path
        existing = {}
        if destination.exists():
            for entry in _query_log_entries(destination, public_path=public_path):
                relative = entry.get("raw_relative_path")
                if isinstance(relative, str):
                    if relative in existing:
                        raise ArtifactImportError(
                            f"repository query log duplicates {relative}"
                        )
                    existing[relative] = entry
        for entry in sorted(entries, key=lambda item: item["fetched_at"]):
            relative = entry["raw_relative_path"]
            if relative in existing:
                if existing[relative] != entry:
                    raise ArtifactImportError(
                        f"repository query log conflicts for {relative}"
                    )
                continue
            _append_query_log(destination, entry)
            existing[relative] = entry


def _validate_repository_destinations(
    validated: Dict[str, Any],
    *,
    repository_root: Path,
) -> None:
    """Reject known destination conflicts before creating any repository file."""
    for snapshot in validated["snapshots"]:
        destination = repository_root / snapshot["public_path"]
        if destination.exists() and destination.read_bytes() != snapshot["content"]:
            raise ArtifactImportError(
                f"refusing to overwrite different file: {snapshot['public_path']}"
            )

    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for snapshot in validated["snapshots"]:
        grouped.setdefault(snapshot["query_log_path"], {})[
            snapshot["raw_relative_path"]
        ] = snapshot["query_log_entry"]
    for public_path, incoming in sorted(grouped.items()):
        destination = repository_root / public_path
        if not destination.exists():
            continue
        existing: Dict[str, Dict[str, Any]] = {}
        for entry in _query_log_entries(destination, public_path=public_path):
            relative = entry.get("raw_relative_path")
            if not isinstance(relative, str) or not relative:
                raise ArtifactImportError(
                    f"repository query log entry has no path: {public_path}"
                )
            if relative in existing:
                raise ArtifactImportError(
                    f"repository query log duplicates {relative}"
                )
            existing[relative] = entry
        for relative, entry in incoming.items():
            if relative in existing and existing[relative] != entry:
                raise ArtifactImportError(
                    f"repository query log conflicts for {relative}"
                )


def _report_content(validated: Dict[str, Any]) -> Dict[str, Any]:
    audit = validated["audit"]
    return {
        "collection_import_schema_version": 1,
        "status": "verified_in_repository",
        "collection_artifact_audit_sha256": validated["audit_sha256"],
        "collection_created_at": audit["created_at"],
        "collection_status": audit["status"],
        "job_id": audit["preflight"]["job_id"],
        "policy_version": audit["preflight"]["policy_version"],
        "workflow_context": audit.get("workflow_context", {}),
        "raw_snapshot_count": len(validated["snapshots"]),
        "raw_snapshots": [item["public_path"] for item in validated["snapshots"]],
        "query_logs": validated["query_logs"],
        "immutable_raw_verified": True,
        "credential_value_persisted": False,
    }


def import_artifact(
    artifact_path: Path,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
    dry_run: bool = False,
) -> Dict[str, Any]:
    repository_root = Path(repository_root).resolve()
    reports_root = repository_root / repository_relative_path(
        reports_root, repository_root=repository_root
    )
    validated = validate_artifact(
        artifact_path, repository_root=repository_root
    )
    report = _report_content(validated)
    if dry_run:
        return {"dry_run": True, "report": report, "report_path": None}

    created = validated["created"]
    report_id = validated["audit_sha256"][:20]
    destination = reports_root.joinpath(
        created.strftime("%Y"),
        created.strftime("%m"),
        created.strftime("%d"),
        report_id + ".json",
    )
    repository_relative_path(destination, repository_root=repository_root)
    content = (
        json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode("utf-8")
    _validate_repository_destinations(
        validated, repository_root=repository_root
    )
    if destination.exists() and destination.read_bytes() != content:
        raise ArtifactImportError(
            "repository collection import report conflicts for " + report_id
        )

    for snapshot in validated["snapshots"]:
        _write_exclusive_or_verify(
            repository_root / snapshot["public_path"], snapshot["content"]
        )
    _merge_query_logs(validated, repository_root=repository_root)
    for snapshot in validated["snapshots"]:
        if (repository_root / snapshot["public_path"]).read_bytes() != snapshot["content"]:
            raise ArtifactImportError(
                f"repository Raw verification failed: {snapshot['public_path']}"
            )

    _write_exclusive_or_verify(destination, content)
    return {"dry_run": False, "report": report, "report_path": destination}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and import one guarded financial collection artifact."
    )
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--reports-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    reports_root = args.reports_root or root / "reports/daily/collection-imports"
    try:
        result = import_artifact(
            args.artifact,
            repository_root=root,
            reports_root=reports_root,
            dry_run=args.dry_run,
        )
        printable = dict(result["report"])
        printable["dry_run"] = result["dry_run"]
        printable["report_path"] = (
            repository_relative_path(result["report_path"], repository_root=root)
            if result["report_path"] is not None
            else None
        )
        print(json.dumps(printable, ensure_ascii=False, indent=2))
        return 0
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
