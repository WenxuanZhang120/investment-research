#!/usr/bin/env python3
"""Validate a Codex collection artifact, preserve Raw, then normalize it."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.audit_iwencai_response import extract_table_components  # noqa: E402
from scripts.normalize_iwencai_announcements import (  # noqa: E402
    build_events,
    write_bundle as write_event_bundle,
)
from scripts.normalize_iwencai_financials import (  # noqa: E402
    build_financial_batch,
    write_financial_bundle,
)
from scripts.normalize_iwencai_etfs import (  # noqa: E402
    build_etf_batch,
    write_etf_bundle,
)
from scripts.normalize_iwencai_market import (  # noqa: E402
    build_normalized_batch,
    write_normalized_bundle,
)
from scripts.normalize_iwencai_news import (  # noqa: E402
    build_news,
    write_bundle as write_news_bundle,
)
from scripts.repository_paths import repository_relative_path  # noqa: E402
from scripts.save_raw_response import (  # noqa: E402
    DEFAULT_RAW_ROOT,
    PROJECT_TIMEZONE,
    save_raw_response,
)


ARTIFACT_SCHEMA_VERSION = 1
IMPORT_SCHEMA_VERSION = 1
DATASET_KINDS = {"market", "etf", "financial", "announcements", "news"}
SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
COLLECTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SECURITY_CODE_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ)$")
SENSITIVE_KEYS = {
    "authorization",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "cookie",
    "password",
    "credential",
    "credentials",
    "secret",
    "secrets",
}
DEFAULT_REPORTS_ROOT = REPOSITORY_ROOT / "reports" / "daily" / "codex-collection-runs"


class CodexCollectionError(ValueError):
    """Raised when an agent collection artifact is unsafe or ambiguous."""


class MissingRawFieldsError(CodexCollectionError):
    """Raised when a raw response exposes no source data fields."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CodexCollectionError(f"{label} must be an ISO 8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise CodexCollectionError(f"{label} is not a valid ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CodexCollectionError(f"{label} must include a timezone offset")
    return parsed.astimezone(PROJECT_TIMEZONE)


def _date(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise CodexCollectionError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise CodexCollectionError(f"{label} must be YYYY-MM-DD") from error


def _reject_sensitive_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower().replace("-", "_")
            if key_text in SENSITIVE_KEYS or key_text.endswith(
                ("_password", "_secret", "_cookie", "_token", "_api_key")
            ):
                raise CodexCollectionError(
                    f"collection artifact contains a forbidden credential field: {path}.{key}"
                )
            _reject_sensitive_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, path=f"{path}[{index}]")


def extract_raw_field_names(payload: Dict[str, Any]) -> List[str]:
    """Extract source field names without assigning canonical meanings."""
    fields: Set[str] = set()
    for component in extract_table_components(payload):
        data = component.get("data")
        if not isinstance(data, dict):
            continue
        for column in data.get("columns", []):
            if not isinstance(column, dict):
                continue
            name = column.get("key") or column.get("index_name")
            if isinstance(name, str) and name:
                fields.add(name)
    if fields:
        return sorted(fields)

    for key in ("datas", "data"):
        rows = payload.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    fields.update(
                        name for name in row if isinstance(name, str) and name
                    )
    tables = payload.get("tables")
    if isinstance(tables, list):
        for item in tables:
            if not isinstance(item, dict):
                continue
            table = item.get("table")
            if isinstance(table, dict):
                fields.update(name for name in table if isinstance(name, str) and name)
    indicators = payload.get("indicators")
    if isinstance(indicators, list):
        fields.update(name for name in indicators if isinstance(name, str) and name)
    if not fields:
        raise MissingRawFieldsError(
            "raw response contains no detectable source fields"
        )
    return sorted(fields)


def _is_successful_empty_search_response(
    payload: Dict[str, Any], *, dataset_kind: str
) -> bool:
    """Return whether a search endpoint explicitly reported a successful empty set."""
    total = payload.get("total")
    return (
        dataset_kind in {"announcements", "news"}
        and payload.get("status_code") == 0
        and isinstance(total, int)
        and not isinstance(total, bool)
        and total == 0
        and payload.get("data") == []
    )


def _extract_dataset_raw_field_names(
    payload: Dict[str, Any], *, dataset_kind: str
) -> List[str]:
    try:
        return extract_raw_field_names(payload)
    except MissingRawFieldsError:
        if _is_successful_empty_search_response(
            payload, dataset_kind=dataset_kind
        ):
            return []
        raise


def _validate_collection_scope(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("scope_schema_version") != 1:
        raise CodexCollectionError("collection_scope schema_version must be 1")
    scope_type = value.get("scope_type")
    if scope_type == "market_wide":
        topic_id = value.get("topic_id")
        if not isinstance(topic_id, str) or not topic_id:
            raise CodexCollectionError("market-wide collection scope requires topic_id")
        return {
            "scope_schema_version": 1,
            "scope_type": "market_wide",
            "topic_id": topic_id,
        }
    if scope_type != "p0_securities":
        raise CodexCollectionError("unsupported collection_scope type")
    codes = value.get("target_security_codes")
    if (
        not isinstance(codes, list)
        or not codes
        or any(not isinstance(code, str) or not SECURITY_CODE_PATTERN.fullmatch(code) for code in codes)
        or len(codes) != len(set(codes))
    ):
        raise CodexCollectionError(
            "P0 collection scope requires unique SH/SZ target_security_codes"
        )
    source_manifest = value.get("target_source_manifest")
    if not isinstance(source_manifest, str) or not source_manifest:
        raise CodexCollectionError("P0 collection scope requires target_source_manifest")
    source_path = PurePosixPath(source_manifest)
    if source_path.is_absolute() or ".." in source_path.parts:
        raise CodexCollectionError("target_source_manifest must be repository-relative")
    target_date = value.get("target_as_of_date")
    _date(target_date, label="collection_scope.target_as_of_date")
    if value.get("priority") != "P0":
        raise CodexCollectionError("P0 collection scope priority must be P0")
    result = {
        "scope_schema_version": 1,
        "scope_type": "p0_securities",
        "priority": "P0",
        "target_source_manifest": source_manifest,
        "target_as_of_date": target_date,
        "target_security_codes": list(codes),
    }
    allowed = value.get("allowed_event_types")
    if allowed is not None:
        if (
            not isinstance(allowed, list)
            or not allowed
            or any(not isinstance(item, str) or not item for item in allowed)
            or len(allowed) != len(set(allowed))
        ):
            raise CodexCollectionError("allowed_event_types must contain unique strings")
        result["allowed_event_types"] = list(allowed)
    return result


def load_collection_artifact(path: Path) -> Dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CodexCollectionError(f"cannot read collection artifact: {path}") from error
    if not isinstance(document, dict):
        raise CodexCollectionError("collection artifact root must be an object")
    return document


def validate_collection_artifact(document: Dict[str, Any]) -> Dict[str, Any]:
    _reject_sensitive_keys(document)
    if document.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise CodexCollectionError(
            f"schema_version must be {ARTIFACT_SCHEMA_VERSION}"
        )
    collection_id = document.get("collection_id")
    if not isinstance(collection_id, str) or not COLLECTION_ID_PATTERN.fullmatch(
        collection_id
    ):
        raise CodexCollectionError("collection_id is invalid")
    dataset_kind = document.get("dataset_kind")
    if dataset_kind not in DATASET_KINDS:
        raise CodexCollectionError(
            "dataset_kind must be one of: " + ", ".join(sorted(DATASET_KINDS))
        )
    source = document.get("source")
    if not isinstance(source, str) or not SOURCE_PATTERN.fullmatch(source):
        raise CodexCollectionError("source must be a stable lowercase identifier")
    query = document.get("query")
    if not isinstance(query, str) or not query.strip():
        raise CodexCollectionError("query must be a non-empty string")
    as_of_date = _date(document.get("as_of_date"), label="as_of_date")
    collection_scope = _validate_collection_scope(document.get("collection_scope"))
    collector = document.get("collector")
    if not isinstance(collector, dict):
        raise CodexCollectionError("collector must be an object")
    if collector.get("method") != "codex_agent":
        raise CodexCollectionError("collector.method must be codex_agent")
    tool = collector.get("tool")
    if not isinstance(tool, str) or not tool:
        raise CodexCollectionError("collector.tool must be a non-empty string")
    if collector.get("raw_response_unmodified") is not True:
        raise CodexCollectionError("collector must confirm raw_response_unmodified=true")

    responses = document.get("responses")
    if not isinstance(responses, list) or not responses:
        raise CodexCollectionError("responses must be a non-empty array")
    normalized_responses = []
    seen = set()
    for index, response in enumerate(responses):
        if not isinstance(response, dict):
            raise CodexCollectionError(f"responses[{index}] must be an object")
        fetched_at = _timestamp(
            response.get("fetched_at"), label=f"responses[{index}].fetched_at"
        )
        raw_response = response.get("raw_response")
        if not isinstance(raw_response, dict):
            raise CodexCollectionError(
                f"responses[{index}].raw_response must be an object"
            )
        detected_fields = _extract_dataset_raw_field_names(
            raw_response, dataset_kind=dataset_kind
        )
        declared_fields = response.get("raw_field_names")
        if (
            not isinstance(declared_fields, list)
            or any(not isinstance(name, str) or not name for name in declared_fields)
            or len(declared_fields) != len(set(declared_fields))
        ):
            raise CodexCollectionError(
                f"responses[{index}].raw_field_names must be unique strings"
            )
        if sorted(declared_fields) != detected_fields:
            raise CodexCollectionError(
                f"responses[{index}].raw_field_names do not match the raw response"
            )
        identity = (fetched_at.isoformat(timespec="microseconds"), _sha256(raw_response))
        if identity in seen:
            raise CodexCollectionError("responses must not contain duplicate snapshots")
        seen.add(identity)
        normalized_responses.append(
            {
                "fetched_at": fetched_at,
                "raw_response": raw_response,
                "raw_field_names": detected_fields,
            }
        )
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "collection_id": collection_id,
        "dataset_kind": dataset_kind,
        "source": source,
        "query": query,
        "as_of_date": as_of_date,
        "collection_scope": collection_scope,
        "collector": {
            "method": "codex_agent",
            "tool": tool,
            "raw_response_unmodified": True,
        },
        "responses": normalized_responses,
    }


def _existing_snapshot(
    *,
    raw_root: Path,
    source: str,
    query: str,
    fetched_at: datetime,
    payload_sha256: str,
    collection_scope: Optional[Dict[str, Any]],
) -> Optional[Path]:
    directory = raw_root.joinpath(
        source,
        fetched_at.strftime("%Y"),
        fetched_at.strftime("%m"),
        fetched_at.strftime("%d"),
    )
    for path in sorted(directory.glob("*.json")):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metadata = envelope.get("metadata") if isinstance(envelope, dict) else None
        if not isinstance(metadata, dict):
            continue
        if (
            metadata.get("source") == source
            and metadata.get("query") == query
            and metadata.get("fetched_at")
            == fetched_at.isoformat(timespec="microseconds")
            and metadata.get("payload_sha256") == payload_sha256
            and metadata.get("collection_scope") == collection_scope
        ):
            return path
    return None


def _save_responses(
    collection: Dict[str, Any],
    *,
    raw_root: Path,
) -> List[Path]:
    paths = []
    for response in collection["responses"]:
        payload_hash = _sha256(response["raw_response"])
        existing = _existing_snapshot(
            raw_root=raw_root,
            source=collection["source"],
            query=collection["query"],
            fetched_at=response["fetched_at"],
            payload_sha256=payload_hash,
            collection_scope=collection.get("collection_scope"),
        )
        if existing is not None:
            paths.append(existing)
            continue
        paths.append(
            save_raw_response(
                response["raw_response"],
                source=collection["source"],
                query=collection["query"],
                raw_root=raw_root,
                fetched_at=response["fetched_at"],
                as_of_date=collection["as_of_date"],
                raw_field_names=response["raw_field_names"],
                collection_method="codex_agent",
                collector_name=collection["collector"]["tool"],
                collection_scope=collection.get("collection_scope"),
            )
        )
    return paths


def normalize_collection(
    collection: Dict[str, Any],
    snapshots: Sequence[Path],
    *,
    repository_root: Path,
) -> List[Path]:
    kind = collection["dataset_kind"]
    normalized_root = repository_root / "data" / "normalized"
    if kind == "market":
        built = build_normalized_batch(
            snapshots, repository_root=repository_root
        )
        fetched = datetime.fromisoformat(built["metadata"]["fetched_at_start"])
        destination = normalized_root.joinpath(
            "runs",
            built["metadata"]["source"],
            fetched.strftime("%Y"),
            fetched.strftime("%m"),
            fetched.strftime("%d"),
            built["metadata"]["record_id"],
        )
        if not (destination / "manifest.json").is_file():
            destination = write_normalized_bundle(
                built, normalized_root=normalized_root
            )
        return [destination]
    if kind == "financial":
        built = build_financial_batch(
            snapshots, repository_root=repository_root
        )
        fetched = datetime.fromisoformat(built["metadata"]["fetched_at_start"])
        destination = normalized_root.joinpath(
            "runs",
            built["metadata"]["source"],
            fetched.strftime("%Y"),
            fetched.strftime("%m"),
            fetched.strftime("%d"),
            built["metadata"]["record_id"],
        )
        if not (destination / "manifest.json").is_file():
            destination = write_financial_bundle(
                built, normalized_root=normalized_root
            )
        return [destination]
    if kind == "etf":
        built = build_etf_batch(
            snapshots, repository_root=repository_root
        )
        fetched = datetime.fromisoformat(built["fetched_at_start"])
        destination = normalized_root.joinpath(
            "runs",
            built["source"],
            fetched.strftime("%Y"),
            fetched.strftime("%m"),
            fetched.strftime("%d"),
            built["bundle_id"],
        )
        if not (destination / "manifest.json").is_file():
            destination = write_etf_bundle(
                built, normalized_root=normalized_root
            )
        return [destination]
    outputs = []
    for snapshot in snapshots:
        if kind == "announcements":
            built = build_events(snapshot, repository_root=repository_root)
            fetched = datetime.fromisoformat(built["metadata"]["fetched_at"])
            destination = normalized_root.joinpath(
                "runs", "iwencai", fetched.strftime("%Y"), fetched.strftime("%m"),
                fetched.strftime("%d"), built["bundle_id"]
            )
            if not (destination / "manifest.json").is_file():
                destination = write_event_bundle(
                    built, normalized_root=normalized_root
                )
            outputs.append(destination)
        else:
            built = build_news(snapshot, repository_root=repository_root)
            fetched = datetime.fromisoformat(built["metadata"]["fetched_at"])
            destination = normalized_root.joinpath(
                "runs", "iwencai", fetched.strftime("%Y"), fetched.strftime("%m"),
                fetched.strftime("%d"), built["bundle_id"]
            )
            if not (destination / "manifest.json").is_file():
                destination = write_news_bundle(
                    built, normalized_root=normalized_root
                )
            outputs.append(destination)
    return outputs


def _public_paths(
    paths: Iterable[Path], *, repository_root: Path
) -> List[str]:
    return [
        repository_relative_path(path, repository_root=repository_root)
        for path in paths
    ]


def _write_audit(
    audit: Dict[str, Any],
    *,
    reports_root: Path,
    repository_root: Path,
) -> Path:
    fetched = datetime.fromisoformat(audit["fetched_at_end"])
    audit_id = hashlib.sha256(_canonical(audit)).hexdigest()[:20]
    destination = reports_root.joinpath(
        fetched.strftime("%Y"),
        fetched.strftime("%m"),
        fetched.strftime("%d"),
        audit_id,
        "每日采集导入审计.json",
    )
    repository_relative_path(destination, repository_root=repository_root)
    content = json.dumps(audit, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if destination.exists():
        if destination.read_text(encoding="utf-8") != content:
            raise CodexCollectionError(f"existing import audit differs: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def import_collection(
    artifact_path: Path,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    raw_root: Optional[Path] = None,
    reports_root: Optional[Path] = None,
    process: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    repository_root = Path(repository_root)
    raw_root = Path(raw_root) if raw_root is not None else repository_root / "data/raw"
    reports_root = (
        Path(reports_root)
        if reports_root is not None
        else repository_root / "reports/daily/codex-collection-runs"
    )
    repository_relative_path(raw_root, repository_root=repository_root)
    repository_relative_path(reports_root, repository_root=repository_root)
    collection = validate_collection_artifact(load_collection_artifact(artifact_path))
    fetched_values = [item["fetched_at"] for item in collection["responses"]]
    audit = {
        "import_schema_version": IMPORT_SCHEMA_VERSION,
        "collection_id": collection["collection_id"],
        "dataset_kind": collection["dataset_kind"],
        "source": collection["source"],
        "query": collection["query"],
        "as_of_date": collection["as_of_date"],
        "collection_scope": collection.get("collection_scope"),
        "collector": collection["collector"],
        "fetched_at_start": min(fetched_values).isoformat(timespec="microseconds"),
        "fetched_at_end": max(fetched_values).isoformat(timespec="microseconds"),
        "response_count": len(collection["responses"]),
        "raw_first_preserved": not dry_run,
        "processing_requested": process,
        "dry_run": dry_run,
        "raw_snapshots": [],
        "normalized_outputs": [],
        "investment_judgment_included": False,
        "automatic_trading_enabled": False,
        "credential_value_persisted": False,
    }
    if dry_run:
        return audit
    snapshots = _save_responses(collection, raw_root=raw_root)
    audit["raw_snapshots"] = _public_paths(
        snapshots, repository_root=repository_root
    )
    outputs = (
        normalize_collection(
            collection,
            snapshots,
            repository_root=repository_root,
        )
        if process
        else []
    )
    audit["normalized_outputs"] = _public_paths(
        outputs, repository_root=repository_root
    )
    audit_path = _write_audit(
        audit,
        reports_root=reports_root,
        repository_root=repository_root,
    )
    return {
        **audit,
        "audit_path": repository_relative_path(
            audit_path, repository_root=repository_root
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="导入 Codex 采集结果，先保存 Raw，再交给现有 Python 标准化流程。"
    )
    parser.add_argument("artifact", type=Path, help="Codex 采集产物 JSON")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--raw-only", action="store_true", help="只保存 Raw，不执行标准化")
    parser.add_argument("--dry-run", action="store_true", help="只验证，不写入文件")
    args = parser.parse_args(argv)
    try:
        result = import_collection(
            args.artifact,
            repository_root=args.root,
            process=not args.raw_only,
            dry_run=args.dry_run,
        )
    except (CodexCollectionError, OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
