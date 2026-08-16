#!/usr/bin/env python3
"""Build deterministic, privacy-filtered JSON for the public static site.

The exporter deliberately reads only normalized, derived-screening and report
artifacts.  Raw collection responses and user-specific directories are outside
its discovery roots and are never copied into the public payload.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.public_payload_safety import (  # noqa: E402
    assert_public_payload_safe,
)


SCHEMA_VERSION = 1
SHARD_NAMES = tuple("0123456789abcdef")
MAX_QUEUE_SHARD_BYTES = 250 * 1024
SECURITY_CODE_PATTERN = re.compile(r"^(?P<digits>[0-9]{6})\.(?P<exchange>[A-Z]{2})$")

FORBIDDEN_DISCOVERY_PARTS = frozenset(
    {
        "raw",
        "portfolio",
        "decision_journal",
        "private",
        ".codex-collection-inbox",
        "inbox",
    }
)
PRIVATE_REPOSITORY_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9._~-])(?:data/raw|portfolio|decision_journal|private|"
    r"\.codex-collection-inbox|inbox)(?:/|\\)"
)
HTTP_URL_PATTERN = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")
MARKET_FIELDS = (
    "as_of_date",
    "security_code",
    "security_name",
    "eligible",
    "eligibility_reasons",
    "priority",
    "rank",
    "score",
    "score_components",
    "market_cap",
    "pe_ttm",
    "net_profit_margin",
    "operating_cash_flow_margin",
    "financial_period_end",
    "financial_available_from",
)
FINANCIAL_REPORT_FIELDS = (
    "security_code",
    "security_name",
    "period_end",
    "report_type",
    "report_period_label",
    "filing_date",
    "available_from",
    "fact_count",
    "present_fact_count",
    "missing_fact_count",
)
FINANCIAL_FACT_FIELDS = (
    "security_code",
    "security_name",
    "period_end",
    "report_type",
    "report_period_label",
    "filing_date",
    "available_from",
    "canonical_field_name",
    "statement_type",
    "value",
    "unit",
    "value_nature",
    "value_status",
)
CONTENT_FIELDS = (
    "news_id",
    "event_id",
    "event_type",
    "published_at",
    "available_from",
    "publisher",
    "security_code",
    "source_security_code",
    "security_name",
    "title",
    "summary",
    "url",
    "classification_keywords",
)
ETF_FIELDS = (
    "universe_id",
    "etf_code",
    "etf_name",
    "exchange",
    "as_of_date",
    "tracked_index",
    "tracked_index_family",
    "fund_type",
    "fund_type_memberships",
    "listing_date",
    "listing_status",
    "price",
    "change_pct",
    "volume",
    "turnover",
    "fund_size",
    "nav",
    "premium_discount_rate",
    "management_fee_rate",
    "custody_fee_rate",
    "tracking_error",
)


class ExportError(ValueError):
    """Raised when a source cannot be verified or safely published."""


def _json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ExportError("public payload is not canonical JSON") from error


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _strict_json_loads(content: str | bytes) -> Any:
    def object_from_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ExportError("selected source JSON contains duplicate object keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise ExportError("selected source JSON contains a non-finite number")

    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        return json.loads(
            text,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except ExportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExportError("selected source is not strict UTF-8 JSON") from error


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ExportError("a selected source JSON file is unreadable") from error
    value = _strict_json_loads(content)
    if not isinstance(value, dict):
        raise ExportError("a selected source JSON root is not an object")
    return value


def _relative_path(path: Path, repository_root: Path) -> str:
    root = repository_root.resolve()
    try:
        relative = path.resolve().relative_to(root)
    except (OSError, ValueError) as error:
        raise ExportError("a selected source is outside the repository") from error
    if any(part.casefold() in FORBIDDEN_DISCOVERY_PARTS for part in relative.parts):
        raise ExportError("a selected source is in a forbidden repository area")
    return relative.as_posix()


def _resolve_reference(
    reference: Any,
    *,
    repository_root: Path,
    base_directory: Optional[Path] = None,
    allowed_prefixes: Sequence[str],
) -> Path:
    if not isinstance(reference, str) or not reference.strip():
        raise ExportError("a selected manifest contains an invalid file reference")
    candidate = Path(reference)
    if candidate.is_absolute():
        raise ExportError("a selected manifest contains an absolute file reference")
    if base_directory is not None and len(candidate.parts) == 1:
        candidate = base_directory / candidate
    else:
        candidate = repository_root / candidate
    relative = _relative_path(candidate, repository_root)
    if not any(
        relative == prefix.rstrip("/") or relative.startswith(prefix.rstrip("/") + "/")
        for prefix in allowed_prefixes
    ):
        raise ExportError("a selected manifest references a disallowed repository area")
    if not candidate.resolve().is_file():
        raise ExportError("a selected manifest references a missing file")
    return candidate.resolve()


def _read_verified_jsonl(
    manifest_path: Path,
    table: Mapping[str, Any],
    *,
    repository_root: Path,
    allowed_prefixes: Sequence[str],
) -> Tuple[List[Dict[str, Any]], Path, bytes]:
    table_path = _resolve_reference(
        table.get("file"),
        repository_root=repository_root,
        base_directory=manifest_path.parent,
        allowed_prefixes=allowed_prefixes,
    )
    try:
        content = table_path.read_bytes()
    except OSError as error:
        raise ExportError("a selected source table is unreadable") from error
    expected_hash = table.get("sha256")
    if not isinstance(expected_hash, str) or _sha256(content) != expected_hash:
        raise ExportError("selected source table sha256 mismatch")

    records: List[Dict[str, Any]] = []
    try:
        for raw_line in content.splitlines():
            if not raw_line.strip():
                continue
            record = _strict_json_loads(raw_line)
            if not isinstance(record, dict):
                raise ExportError("a selected source table row is not an object")
            records.append(record)
    except ExportError:
        raise
    expected_count = table.get("record_count")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise ExportError("selected source table has an invalid record_count")
    if len(records) != expected_count:
        raise ExportError("selected source table record_count mismatch")
    return records, table_path, content


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00+00:00"
    elif text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _date_from_path(path: Path) -> str:
    parts = path.parts
    for index in range(len(parts) - 2):
        candidate = "/".join(parts[index : index + 3])
        if re.fullmatch(r"\d{4}/\d{2}/\d{2}", candidate):
            return candidate.replace("/", "-")
    return ""


def _version_key(value: Any) -> Tuple[Tuple[int, Any], ...]:
    if not isinstance(value, str):
        return ()
    result: List[Tuple[int, Any]] = []
    for part in re.split(r"[._+-]", value.casefold()):
        if part.isdigit():
            result.append((1, int(part)))
        else:
            result.append((0, part))
    return tuple(result)


def _source_date(path: Path, manifest: Mapping[str, Any]) -> str:
    for key in (
        "as_of_date",
        "fetched_at_end",
        "fetched_at",
        "finished_at",
        "started_at",
    ):
        value = manifest.get(key)
        if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
            return value[:10]
    return _date_from_path(path)


def _validated_iso_date(value: Any, error_message: str) -> str:
    if not isinstance(value, str):
        raise ExportError(error_message)
    try:
        normalized = datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as error:
        raise ExportError(error_message) from error
    if normalized != value:
        raise ExportError(error_message)
    return value


def _camel_name(name: str) -> str:
    pieces = name.split("_")
    return pieces[0] + "".join(piece[:1].upper() + piece[1:] for piece in pieces[1:])


def _whitelist(record: Mapping[str, Any], fields: Sequence[str]) -> Dict[str, Any]:
    return {
        _camel_name(field): record[field]
        for field in fields
        if field in record
    }


def _safe_public_payload(payload: Any) -> None:
    assert_public_payload_safe(payload)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
            return
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, str):
            return
        visible_parts = []
        cursor = 0
        for match in HTTP_URL_PATTERN.finditer(value):
            visible_parts.append(value[cursor : match.start()])
            cursor = match.end()
        visible_parts.append(value[cursor:])
        if PRIVATE_REPOSITORY_PATH_PATTERN.search(" ".join(visible_parts)):
            raise ExportError("public site payload contains a forbidden private repository path")

    visit(payload)


def _domain_status(source_count: int, record_count: int) -> str:
    if source_count == 0:
        return "missing"
    if record_count == 0:
        return "empty"
    return "ready"


def _discover_screening(repository_root: Path) -> Dict[str, Any]:
    base = repository_root / "data/derived/runs/screening"
    candidates: List[Tuple[Tuple[Any, ...], Path, Dict[str, Any]]] = []
    if base.is_dir():
        for path in sorted(base.glob("**/manifest.json")):
            if "github_connector" in path.parts:
                continue
            manifest = _read_json(path)
            table = manifest.get("table")
            if not isinstance(table, dict) or table.get("logical_name") != "market_research_queue":
                continue
            key = (
                _source_date(path, manifest),
                _version_key(manifest.get("screening_version")),
                _version_key(manifest.get("screener_version")),
                str(manifest.get("bundle_id") or ""),
            )
            candidates.append((key, path, manifest))
    if not candidates:
        return {"manifest": None, "path": None, "records": [], "as_of_date": None}

    _, manifest_path, manifest = max(candidates, key=lambda item: item[0])
    table = manifest["table"]
    records, _, _ = _read_verified_jsonl(
        manifest_path,
        table,
        repository_root=repository_root,
        allowed_prefixes=("data/derived/runs/screening",),
    )
    universe_path = repository_root / "config/investment_universe.json"
    universe = _read_json(universe_path)
    stocks = universe.get("stocks")
    universe_version = universe.get("universe_version")
    stock_universe_id = stocks.get("universe_id") if isinstance(stocks, dict) else None
    minimum_expected_count = (
        stocks.get("minimum_expected_count") if isinstance(stocks, dict) else None
    )
    allowed_exchanges = (
        stocks.get("allowed_exchanges") if isinstance(stocks, dict) else None
    )
    excluded_prefixes = (
        stocks.get("excluded_code_prefixes") if isinstance(stocks, dict) else None
    )
    if not isinstance(universe_version, str) or not universe_version:
        raise ExportError("investment universe universe_version is missing or invalid")
    if not isinstance(stock_universe_id, str) or not stock_universe_id:
        raise ExportError("investment universe stocks.universe_id is missing or invalid")
    if (
        isinstance(minimum_expected_count, bool)
        or not isinstance(minimum_expected_count, int)
        or minimum_expected_count <= 0
    ):
        raise ExportError(
            "investment universe stocks.minimum_expected_count is missing or invalid"
        )
    if (
        not isinstance(allowed_exchanges, list)
        or not allowed_exchanges
        or any(not isinstance(exchange, str) or not exchange for exchange in allowed_exchanges)
    ):
        raise ExportError("investment universe stocks.allowed_exchanges is missing or invalid")
    if (
        not isinstance(excluded_prefixes, list)
        or any(not isinstance(prefix, str) or not prefix for prefix in excluded_prefixes)
    ):
        raise ExportError(
            "investment universe stocks.excluded_code_prefixes is missing or invalid"
        )
    if manifest.get("investment_universe") != "config/investment_universe.json":
        raise ExportError("selected screening investment_universe path mismatch")
    if manifest.get("universe_version") != universe_version:
        raise ExportError("selected screening universe_version mismatch")
    if manifest.get("universe_id") != stock_universe_id:
        raise ExportError("selected screening universe_id mismatch")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, dict):
        raise ExportError("selected screening coverage is missing or invalid")
    if coverage.get("configured_stock_universe_id") != stock_universe_id:
        raise ExportError("selected screening configured universe_id mismatch")
    primary_key = table.get("primary_key")
    if not isinstance(primary_key, list) or "security_code" not in primary_key:
        raise ExportError("selected screening primary key omits security_code")
    if len(records) < minimum_expected_count:
        raise ExportError(
            "selected screening table is below stocks.minimum_expected_count"
        )
    universe_count = coverage.get("universe_count")
    if isinstance(universe_count, bool) or not isinstance(universe_count, int):
        raise ExportError("selected screening coverage count is invalid")
    if universe_count != len(records):
        raise ExportError("selected screening coverage count mismatch")

    seen_codes = set()
    allowed_exchange_set = set(allowed_exchanges)
    for record in records:
        security_code = record.get("security_code")
        match = (
            SECURITY_CODE_PATTERN.fullmatch(security_code)
            if isinstance(security_code, str)
            else None
        )
        if match is None or match.group("exchange") not in allowed_exchange_set:
            raise ExportError("selected screening table contains an invalid security_code")
        if any(match.group("digits").startswith(prefix) for prefix in excluded_prefixes):
            raise ExportError(
                "selected screening table contains a security_code outside the configured universe"
            )
        if security_code in seen_codes:
            raise ExportError("selected screening table contains duplicate security_code values")
        seen_codes.add(security_code)
    source_date = _validated_iso_date(
        _source_date(manifest_path, manifest),
        "selected screening source has no valid as_of_date",
    )
    explicit_manifest_date = manifest.get("as_of_date")
    if explicit_manifest_date is not None and _validated_iso_date(
        explicit_manifest_date,
        "selected screening manifest has an invalid as_of_date",
    ) != source_date:
        raise ExportError("selected screening manifest as_of_date mismatch")
    path_date = _date_from_path(manifest_path)
    if path_date and path_date != source_date:
        raise ExportError("selected screening path date mismatch")
    for record in records:
        record_date = _validated_iso_date(
            record.get("as_of_date"),
            "selected screening table has a missing or invalid as_of_date",
        )
        if record_date != source_date:
            raise ExportError("selected screening table as_of_date mismatch")
    return {
        "manifest": manifest,
        "path": manifest_path,
        "records": records,
        "as_of_date": source_date,
    }


def _classify_normalized_manifest(manifest: Mapping[str, Any]) -> Optional[str]:
    tables = manifest.get("tables")
    if isinstance(tables, dict) and "etf_snapshots" in tables:
        return "etf"
    if isinstance(tables, dict) and any(
        name in tables for name in ("financial_reports", "financial_facts")
    ):
        return "financial"
    table = manifest.get("table")
    if isinstance(table, dict):
        logical_name = table.get("logical_name")
        if logical_name == "news_items":
            return "news"
        if logical_name == "events":
            return "events"
    return None


def _discover_normalized(repository_root: Path) -> Dict[str, Dict[str, Any]]:
    base = repository_root / "data/normalized/runs"
    candidates: Dict[str, List[Tuple[str, Path, Dict[str, Any]]]] = {
        "etf": [],
        "financial": [],
        "news": [],
        "events": [],
    }
    if base.is_dir():
        for path in sorted(base.glob("**/manifest.json")):
            manifest = _read_json(path)
            domain = _classify_normalized_manifest(manifest)
            if domain is None:
                continue
            candidates[domain].append((_source_date(path, manifest), path, manifest))

    result: Dict[str, Dict[str, Any]] = {}
    for domain, domain_candidates in candidates.items():
        if not domain_candidates:
            result[domain] = {"date": None, "sources": [], "records": {}}
            continue
        latest_date = max(candidate[0] for candidate in domain_candidates)
        selected = sorted(
            (candidate for candidate in domain_candidates if candidate[0] == latest_date),
            key=lambda candidate: candidate[1].as_posix(),
        )
        source_entries: List[Dict[str, Any]] = []
        collected: Dict[str, List[Dict[str, Any]]] = {}
        for _, manifest_path, manifest in selected:
            if domain == "financial":
                tables = manifest.get("tables")
                if not isinstance(tables, dict):
                    raise ExportError("selected financial manifest has no tables")
                selected_tables = {
                    name: descriptor
                    for name, descriptor in tables.items()
                    if name in {"financial_reports", "financial_facts"}
                }
                if not selected_tables:
                    raise ExportError("selected financial manifest has no public table")
            elif domain == "etf":
                tables = manifest.get("tables")
                table = tables.get("etf_snapshots") if isinstance(tables, dict) else None
                if not isinstance(table, dict):
                    raise ExportError("selected ETF manifest has no etf_snapshots table")
                selected_tables = {"etf_snapshots": table}
            else:
                table = manifest.get("table")
                if not isinstance(table, dict):
                    raise ExportError("selected monitoring manifest has no table")
                selected_tables = {str(table.get("logical_name")): table}
            source_record_count = 0
            for logical_name, table in sorted(selected_tables.items()):
                rows, _, _ = _read_verified_jsonl(
                    manifest_path,
                    table,
                    repository_root=repository_root,
                    allowed_prefixes=("data/normalized/runs",),
                )
                collected.setdefault(logical_name, []).extend(rows)
                source_record_count += len(rows)
            source_entries.append(
                {
                    "manifest": manifest,
                    "path": manifest_path,
                    "record_count": source_record_count,
                }
            )
        result[domain] = {
            "date": latest_date or None,
            "sources": source_entries,
            "records": collected,
        }
    return result


def _discover_pipeline(repository_root: Path) -> Dict[str, Any]:
    base = repository_root / "reports/daily/pipeline-runs"
    candidates: List[Tuple[datetime, str, Path, Dict[str, Any]]] = []
    if base.is_dir():
        for path in sorted(base.glob("**/*.json")):
            run = _read_json(path)
            timestamp = _parse_timestamp(run.get("finished_at")) or _parse_timestamp(
                run.get("started_at")
            )
            if timestamp is None:
                timestamp = _parse_timestamp(_date_from_path(path))
            if timestamp is None:
                raise ExportError("a pipeline run has no valid source timestamp")
            candidates.append((timestamp, str(run.get("run_id") or ""), path, run))
    if not candidates:
        return {"path": None, "run": None}
    _, _, path, run = max(candidates, key=lambda item: (item[0], item[1], item[2].as_posix()))
    return {"path": path, "run": run}


def _verify_file_hash(path: Path, expected_hash: Any) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ExportError("a selected report file is unreadable") from error
    if not isinstance(expected_hash, str) or _sha256(content) != expected_hash:
        raise ExportError("selected report sha256 mismatch")
    return content


def _report_title(path: Path, content: bytes) -> str:
    if path.suffix.casefold() == ".md":
        try:
            for line in content.decode("utf-8").splitlines():
                title = line.strip().lstrip("#").strip()
                if line.lstrip().startswith("#") and title:
                    return title
        except UnicodeDecodeError as error:
            raise ExportError("a selected report is not valid UTF-8") from error
    return path.stem.replace("-", " ")


def _discover_reports(repository_root: Path) -> Dict[str, Any]:
    monitoring_base = repository_root / "reports/daily/monitoring"
    by_kind: Dict[str, List[Tuple[str, Path, Dict[str, Any]]]] = {}
    if monitoring_base.is_dir():
        for path in sorted(monitoring_base.glob("**/manifest.json")):
            manifest = _read_json(path)
            kind = manifest.get("kind")
            if not isinstance(kind, str) or not kind:
                continue
            by_kind.setdefault(kind, []).append((_source_date(path, manifest), path, manifest))

    reports: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    latest_dates: List[str] = []
    for kind, candidates in sorted(by_kind.items()):
        latest_date = max(candidate[0] for candidate in candidates)
        latest_dates.append(latest_date)
        for _, manifest_path, manifest in sorted(
            (candidate for candidate in candidates if candidate[0] == latest_date),
            key=lambda candidate: candidate[1].as_posix(),
        ):
            report = manifest.get("report")
            if not isinstance(report, dict):
                raise ExportError("selected report manifest has no report descriptor")
            report_path = _resolve_reference(
                report.get("file"),
                repository_root=repository_root,
                base_directory=manifest_path.parent,
                allowed_prefixes=("reports/daily",),
            )
            content = _verify_file_hash(report_path, report.get("sha256"))
            for source in manifest.get("source_manifests", []):
                if not isinstance(source, dict):
                    raise ExportError("selected report source descriptor is invalid")
                source_path = _resolve_reference(
                    source.get("path"),
                    repository_root=repository_root,
                    allowed_prefixes=("data/normalized/runs", "data/derived/runs"),
                )
                _verify_file_hash(source_path, source.get("sha256"))
            coverage = manifest.get("coverage") if isinstance(manifest.get("coverage"), dict) else {}
            record_count = coverage.get("record_count", 0)
            reports.append(
                {
                    "kind": kind,
                    "title": _report_title(report_path, content),
                    "asOfDate": manifest.get("as_of_date") or latest_date,
                    "path": _relative_path(report_path, repository_root),
                    "sha256": _sha256(content),
                    "bytes": len(content),
                    "recordCount": record_count if isinstance(record_count, int) else None,
                    "status": "empty" if record_count == 0 else "ready",
                }
            )
            sources.append(
                {
                    "manifest": manifest,
                    "path": manifest_path,
                    "record_count": record_count if isinstance(record_count, int) else None,
                }
            )

    daily_base = repository_root / "reports/daily"
    standalone: List[Tuple[str, Path]] = []
    if daily_base.is_dir():
        for path in sorted(daily_base.iterdir()):
            if not path.is_file() or path.suffix.casefold() not in {".md", ".json"}:
                continue
            match = re.match(r"^(\d{4}-\d{2}-\d{2})-", path.name)
            if match:
                standalone.append((match.group(1), path))
    standalone_count = 0
    if standalone:
        latest_date = max(date for date, _ in standalone)
        latest_dates.append(latest_date)
        for _, path in (item for item in standalone if item[0] == latest_date):
            standalone_count += 1
            content = path.read_bytes()
            reports.append(
                {
                    "kind": "daily",
                    "title": _report_title(path, content),
                    "asOfDate": latest_date,
                    "path": _relative_path(path, repository_root),
                    "sha256": _sha256(content),
                    "bytes": len(content),
                    "recordCount": None,
                    "status": "ready",
                }
            )
    reports.sort(key=lambda item: (str(item.get("asOfDate") or ""), item["kind"], item["path"]), reverse=True)
    return {
        "date": max(latest_dates) if latest_dates else None,
        "reports": reports,
        "sources": sources,
        "source_count": len(sources) + standalone_count,
    }


def _deduplicate_content(records: Iterable[Dict[str, Any]], id_field: str) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for record in records:
        identifier = record.get(id_field)
        if not isinstance(identifier, str) or not identifier.strip():
            raise ExportError("selected normalized content has a missing identifier")
        existing = by_id.get(identifier)
        if existing is not None and existing != record:
            raise ExportError("selected normalized content has conflicting duplicate identifiers")
        by_id[identifier] = record
    result = list(by_id.values())
    result.sort(
        key=lambda item: (
            str(item.get("publishedAt") or ""),
            str(item.get(id_field) or ""),
            str(item.get("title") or ""),
        ),
        reverse=True,
    )
    return result


def _public_content_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    public = _whitelist(record, CONTENT_FIELDS)
    if "url" not in public or public["url"] is None:
        return public
    url = public["url"]
    if (
        not isinstance(url, str)
        or not url
        or "\\" in url
        or any(ord(character) < 0x20 or character.isspace() for character in url)
    ):
        raise ExportError("selected content contains an invalid public URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        hostname = parsed.hostname
    except (UnicodeError, ValueError) as error:
        raise ExportError("selected content contains an invalid public URL") from error
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or port is not None and not (1 <= port <= 65535)
    ):
        raise ExportError("selected content URL must be absolute HTTP or HTTPS")
    if parsed.username not in (None, "") or parsed.password not in (None, ""):
        raise ExportError("selected content URL must not contain userinfo")
    normalized_hostname = hostname.rstrip(".").casefold()
    if not normalized_hostname:
        raise ExportError("selected content URL must use a public host")
    try:
        address = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        labels = normalized_hostname.split(".")
        blocked_suffixes = {
            "localhost",
            "local",
            "internal",
            "lan",
            "home",
            "test",
            "invalid",
            "onion",
        }
        if (
            len(labels) < 2
            or any(not label for label in labels)
            or labels[-1] in blocked_suffixes
        ):
            raise ExportError("selected content URL must use a public host")
    else:
        if not address.is_global:
            raise ExportError("selected content URL must use a public host")
    assert_public_payload_safe(url, location="$content.url")
    return public


def _deduplicate_etfs(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for record in records:
        code = record.get("etfCode")
        as_of_date = record.get("asOfDate")
        if not isinstance(code, str) or not code or not isinstance(as_of_date, str) or not as_of_date:
            raise ExportError("selected ETF record has no etf_code/as_of_date")
        key = (code, as_of_date)
        if key in by_key:
            raise ExportError("selected ETF data has duplicate code/date rows")
        by_key[key] = record
    return [by_key[key] for key in sorted(by_key)]


def _validated_public_etfs(
    domain: Mapping[str, Any], repository_root: Path
) -> List[Dict[str, Any]]:
    source_entries = domain.get("sources")
    records_by_table = domain.get("records")
    if not isinstance(source_entries, list) or not isinstance(records_by_table, dict):
        raise ExportError("selected ETF domain is invalid")
    if not source_entries:
        return []

    expected_date = _validated_iso_date(
        domain.get("date"), "selected ETF domain has no valid as_of_date"
    )

    universe = _read_json(repository_root / "config/investment_universe.json")
    etf_config = universe.get("etfs")
    universe_version = universe.get("universe_version")
    expected_universe_id = (
        etf_config.get("universe_id") if isinstance(etf_config, dict) else None
    )
    allowed_exchanges = (
        etf_config.get("allowed_exchanges") if isinstance(etf_config, dict) else None
    )
    if not isinstance(universe_version, str) or not universe_version:
        raise ExportError("investment universe universe_version is missing or invalid")
    if not isinstance(expected_universe_id, str) or not expected_universe_id:
        raise ExportError("investment universe etfs.universe_id is missing or invalid")
    if (
        not isinstance(allowed_exchanges, list)
        or not allowed_exchanges
        or any(not isinstance(exchange, str) or not exchange for exchange in allowed_exchanges)
    ):
        raise ExportError("investment universe etfs.allowed_exchanges is missing or invalid")
    for source in source_entries:
        manifest = source.get("manifest") if isinstance(source, dict) else None
        manifest_path = source.get("path") if isinstance(source, dict) else None
        if not isinstance(manifest, dict):
            raise ExportError("selected ETF manifest is invalid")
        if not isinstance(manifest_path, Path):
            raise ExportError("selected ETF manifest path is invalid")
        if manifest.get("universe_id") != expected_universe_id:
            raise ExportError("selected ETF manifest universe_id mismatch")
        if manifest.get("universe_version") != universe_version:
            raise ExportError("selected ETF manifest universe_version mismatch")
        source_date = _validated_iso_date(
            _source_date(manifest_path, manifest),
            "selected ETF source has no valid as_of_date",
        )
        if source_date != expected_date:
            raise ExportError("selected ETF source as_of_date mismatch")
        manifest_date = manifest.get("as_of_date")
        if manifest_date is not None and _validated_iso_date(
            manifest_date,
            "selected ETF manifest has an invalid as_of_date",
        ) != expected_date:
            raise ExportError("selected ETF manifest as_of_date mismatch")
        path_date = _date_from_path(manifest_path)
        if path_date and path_date != expected_date:
            raise ExportError("selected ETF path date mismatch")

    allowed_exchange_set = set(allowed_exchanges)
    public_records = []
    for record in records_by_table.get("etf_snapshots", []):
        if record.get("universe_id") != expected_universe_id:
            raise ExportError("selected ETF record universe_id mismatch")
        if record.get("universe_version") != universe_version:
            raise ExportError("selected ETF record universe_version mismatch")
        code = record.get("etf_code")
        match = SECURITY_CODE_PATTERN.fullmatch(code) if isinstance(code, str) else None
        exchange = record.get("exchange")
        if (
            match is None
            or exchange not in allowed_exchange_set
            or match.group("exchange") != exchange
        ):
            raise ExportError("selected ETF record has an invalid code/exchange")
        as_of_date = _validated_iso_date(
            record.get("as_of_date"),
            "selected ETF record has an invalid as_of_date",
        )
        if as_of_date != expected_date:
            raise ExportError("selected ETF record as_of_date mismatch")
        public_records.append(_whitelist(record, ETF_FIELDS))
    return _deduplicate_etfs(public_records)


def _shard_for_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[0]


def _build_pipeline_status(pipeline: Mapping[str, Any]) -> Dict[str, Any]:
    path = pipeline.get("path")
    run = pipeline.get("run")
    if not isinstance(path, Path) or not isinstance(run, dict):
        return {
            "schemaVersion": SCHEMA_VERSION,
            "status": "missing",
            "artifactAvailable": False,
            "run": None,
        }
    readiness = run.get("readiness") if isinstance(run.get("readiness"), dict) else {}
    research = readiness.get("research") if isinstance(readiness.get("research"), dict) else {}
    screening = research.get("screening") if isinstance(research.get("screening"), dict) else {}
    monitoring = research.get("monitoring") if isinstance(research.get("monitoring"), dict) else {}
    steps = []
    for step in run.get("steps", []):
        if not isinstance(step, dict):
            continue
        steps.append(
            {
                key: step.get(source_key)
                for key, source_key in (
                    ("stage", "stage"),
                    ("stepId", "step_id"),
                    ("status", "status"),
                    ("exitCode", "exit_code"),
                    ("errorType", "error_type"),
                    ("startedAt", "started_at"),
                    ("finishedAt", "finished_at"),
                )
            }
        )
    run_status = run.get("status")
    readiness_status = readiness.get("status")
    research_status = research.get("status")
    incomplete_job_count = readiness.get("incomplete_job_count")
    any_failed_step = any(step.get("status") == "failed" for step in steps)
    if run_status != "succeeded" or any_failed_step:
        publication_status = "failed"
    elif (
        readiness_status == "up_to_date"
        and research_status == "up_to_date"
        and isinstance(incomplete_job_count, int)
        and not isinstance(incomplete_job_count, bool)
        and incomplete_job_count == 0
    ):
        publication_status = "ready"
    else:
        publication_status = "partial"
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": publication_status,
        "artifactAvailable": True,
        "run": {
            "runId": run.get("run_id"),
            "pipelineVersion": run.get("pipeline_version"),
            "status": run.get("status"),
            "startedAt": run.get("started_at"),
            "finishedAt": run.get("finished_at"),
            "stepCount": run.get("step_count"),
            "steps": steps,
            "readiness": {
                "status": readiness.get("status"),
                "plannedStepCount": readiness.get("planned_step_count"),
                "incompleteJobCount": readiness.get("incomplete_job_count"),
                "researchStatus": research.get("status"),
                "screeningStatus": screening.get("status"),
                "monitoringMatchedSnapshotCount": monitoring.get("matched_snapshot_count"),
            },
        },
    }


def _provenance_source(
    *,
    domain: str,
    path: Path,
    repository_root: Path,
    manifest: Mapping[str, Any],
    record_count: Optional[int],
) -> Dict[str, Any]:
    content = path.read_bytes()
    return {
        "domain": domain,
        "path": _relative_path(path, repository_root),
        "sha256": _sha256(content),
        "bytes": len(content),
        "bundleId": manifest.get("bundle_id"),
        "runId": manifest.get("run_id"),
        "asOfDate": manifest.get("as_of_date") or _source_date(path, manifest) or None,
        "fetchedAt": manifest.get("fetched_at_end") or manifest.get("fetched_at"),
        "recordCount": record_count,
    }


def _latest_generated_at(values: Iterable[Any]) -> Optional[str]:
    parsed_values: List[Tuple[datetime, str]] = []
    for value in values:
        parsed = _parse_timestamp(value)
        if parsed is not None and isinstance(value, str):
            parsed_values.append((parsed, value))
    if not parsed_values:
        return None
    return max(parsed_values, key=lambda item: (item[0], item[1]))[1]


def build_public_payloads(repository_root: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Return all non-root payloads plus metadata used by ``index.json``."""
    root = repository_root.resolve()
    screening = _discover_screening(root)
    normalized = _discover_normalized(root)
    pipeline = _discover_pipeline(root)
    reports = _discover_reports(root)

    market_records = [
        _whitelist(record, MARKET_FIELDS) for record in screening["records"]
    ]
    market_records.sort(
        key=lambda item: (
            item.get("rank") if isinstance(item.get("rank"), (int, float)) else float("inf"),
            str(item.get("securityCode") or ""),
        )
    )
    market_code_set = {
        record["securityCode"]
        for record in market_records
        if isinstance(record.get("securityCode"), str) and record["securityCode"]
    }

    queue_shards: Dict[str, List[Dict[str, Any]]] = {name: [] for name in SHARD_NAMES}
    for record in market_records:
        key = record.get("securityCode")
        if not isinstance(key, str) or not key:
            key = _json_bytes(record).decode("utf-8")
        queue_shards[_shard_for_key(key)].append(record)

    financial_reports = [
        _whitelist(record, FINANCIAL_REPORT_FIELDS)
        for record in normalized["financial"]["records"].get("financial_reports", [])
        if record.get("security_code") in market_code_set
    ]
    financial_facts = [
        _whitelist(record, FINANCIAL_FACT_FIELDS)
        for record in normalized["financial"]["records"].get("financial_facts", [])
        if record.get("security_code") in market_code_set
    ]
    financial_reports.sort(
        key=lambda item: (str(item.get("securityCode") or ""), str(item.get("periodEnd") or ""))
    )
    financial_facts.sort(
        key=lambda item: (
            str(item.get("securityCode") or ""),
            str(item.get("periodEnd") or ""),
            str(item.get("canonicalFieldName") or ""),
        )
    )

    news = _deduplicate_content(
        (
            _public_content_record(record)
            for record in normalized["news"]["records"].get("news_items", [])
        ),
        "newsId",
    )
    events = _deduplicate_content(
        (
            _public_content_record(record)
            for record in normalized["events"]["records"].get("events", [])
        ),
        "eventId",
    )
    etfs = _validated_public_etfs(normalized["etf"], root)

    companies: MutableMapping[str, Dict[str, Any]] = {
        record["securityCode"]: {
            "securityCode": record["securityCode"],
            "securityName": record.get("securityName"),
            "market": record,
            "financialReports": [],
            "financialFacts": [],
            "newsIds": [],
            "eventIds": [],
        }
        for record in market_records
    }

    def company(code: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(code, str) or not code:
            return None
        return companies.get(code)

    for record in financial_reports:
        current = company(record.get("securityCode"))
        if current is not None:
            current["financialReports"].append(record)
    for record in financial_facts:
        current = company(record.get("securityCode"))
        if current is not None:
            current["financialFacts"].append(record)
    for record in news:
        current = company(record.get("securityCode"))
        identifier = record.get("newsId")
        if current is not None and isinstance(identifier, str):
            current["newsIds"].append(identifier)
    for record in events:
        current = company(record.get("securityCode"))
        identifier = record.get("eventId")
        if current is not None and isinstance(identifier, str):
            current["eventIds"].append(identifier)

    company_details = sorted(companies.values(), key=lambda item: item["securityCode"])
    if len(company_details) != len(market_records):
        raise ExportError("public company universe does not match screening")
    detail_shards: Dict[str, List[Dict[str, Any]]] = {name: [] for name in SHARD_NAMES}
    for record in company_details:
        record["newsIds"].sort()
        record["eventIds"].sort()
        detail_shards[_shard_for_key(record["securityCode"])].append(record)

    company_index = []
    for record in company_details:
        market = record.get("market") if isinstance(record.get("market"), dict) else {}
        company_index.append(
            {
                "securityCode": record["securityCode"],
                "securityName": record.get("securityName"),
                "priority": market.get("priority"),
                "rank": market.get("rank"),
                "hasMarket": bool(market),
                "financialReportCount": len(record["financialReports"]),
                "financialFactCount": len(record["financialFacts"]),
                "newsCount": len(record["newsIds"]),
                "eventCount": len(record["eventIds"]),
                "detailShard": _shard_for_key(record["securityCode"]),
            }
        )

    screening_source_count = 1 if screening["manifest"] is not None else 0
    priority_counts = dict(
        sorted(
            Counter(
                str(record.get("priority"))
                for record in market_records
                if record.get("priority") is not None
            ).items()
        )
    )
    eligible_count = sum(record.get("eligible") is True for record in market_records)
    market_status = _domain_status(screening_source_count, len(market_records))

    payloads: Dict[str, Dict[str, Any]] = {
        "status/latest.json": _build_pipeline_status(pipeline),
        "market/summary.json": {
            "schemaVersion": SCHEMA_VERSION,
            "status": market_status,
            "asOfDate": screening["as_of_date"],
            "bundleId": screening["manifest"].get("bundle_id") if screening["manifest"] else None,
            "screeningVersion": screening["manifest"].get("screening_version") if screening["manifest"] else None,
            "purpose": screening["manifest"].get("purpose") if screening["manifest"] else None,
            "recordCount": len(market_records),
            "eligibleCount": eligible_count,
            "rejectCount": len(market_records) - eligible_count,
            "priorityCounts": priority_counts,
            "shards": [
                {"name": name, "path": f"market/queue-{name}.json", "recordCount": len(queue_shards[name])}
                for name in SHARD_NAMES
            ],
        },
        "companies/index.json": {
            "schemaVersion": SCHEMA_VERSION,
            "status": _domain_status(screening_source_count, len(company_index)),
            "recordCount": len(company_index),
            "companies": company_index,
        },
        "etf/index.json": {
            "schemaVersion": SCHEMA_VERSION,
            "status": _domain_status(len(normalized["etf"]["sources"]), len(etfs)),
            "asOfDate": normalized["etf"]["date"],
            "recordCount": len(etfs),
            "records": etfs,
        },
        "content/index.json": {
            "schemaVersion": SCHEMA_VERSION,
            "status": _domain_status(
                len(normalized["news"]["sources"])
                + len(normalized["events"]["sources"])
                + reports["source_count"],
                len(news) + len(events) + len(reports["reports"]),
            ),
            "domains": {
                "news": {
                    "status": _domain_status(len(normalized["news"]["sources"]), len(news)),
                    "asOfDate": normalized["news"]["date"],
                    "recordCount": len(news),
                },
                "events": {
                    "status": _domain_status(len(normalized["events"]["sources"]), len(events)),
                    "asOfDate": normalized["events"]["date"],
                    "recordCount": len(events),
                },
                "reports": {
                    "status": _domain_status(reports["source_count"], len(reports["reports"])),
                    "asOfDate": reports["date"],
                    "recordCount": len(reports["reports"]),
                },
            },
            "news": news,
            "events": events,
            "reports": reports["reports"],
        },
    }
    for shard in SHARD_NAMES:
        payloads[f"market/queue-{shard}.json"] = {
            "schemaVersion": SCHEMA_VERSION,
            "status": market_status,
            "shard": shard,
            "recordCount": len(queue_shards[shard]),
            "records": queue_shards[shard],
        }
        payloads[f"companies/details-{shard}.json"] = {
            "schemaVersion": SCHEMA_VERSION,
            "status": _domain_status(screening_source_count, len(detail_shards[shard])),
            "shard": shard,
            "recordCount": len(detail_shards[shard]),
            "companies": detail_shards[shard],
        }

    provenance: List[Dict[str, Any]] = []
    source_timestamps: List[Any] = []
    if screening["manifest"] is not None:
        provenance.append(
            _provenance_source(
                domain="screening",
                path=screening["path"],
                repository_root=root,
                manifest=screening["manifest"],
                record_count=len(screening["records"]),
            )
        )
        source_timestamps.append(screening["as_of_date"])
    if pipeline["run"] is not None:
        provenance.append(
            _provenance_source(
                domain="pipeline",
                path=pipeline["path"],
                repository_root=root,
                manifest=pipeline["run"],
                record_count=pipeline["run"].get("step_count"),
            )
        )
        source_timestamps.extend((pipeline["run"].get("finished_at"), pipeline["run"].get("started_at")))
    for domain in ("etf", "financial", "news", "events"):
        for source in normalized[domain]["sources"]:
            provenance.append(
                _provenance_source(
                    domain=domain,
                    path=source["path"],
                    repository_root=root,
                    manifest=source["manifest"],
                    record_count=source["record_count"],
                )
            )
            source_timestamps.extend(
                (
                    source["manifest"].get("fetched_at_end"),
                    source["manifest"].get("fetched_at"),
                    normalized[domain]["date"],
                )
            )
    for source in reports["sources"]:
        provenance.append(
            _provenance_source(
                domain="report",
                path=source["path"],
                repository_root=root,
                manifest=source["manifest"],
                record_count=source["record_count"],
            )
        )
        source_timestamps.append(source["manifest"].get("as_of_date"))
    provenance.sort(key=lambda item: (item["domain"], item["path"]))
    generated_at = _latest_generated_at(source_timestamps)
    payloads["provenance/index.json"] = {
        "schemaVersion": SCHEMA_VERSION,
        "status": "ready" if provenance else "missing",
        "generatedAt": generated_at,
        "sourceCount": len(provenance),
        "sources": provenance,
    }

    domain_summaries = {
        "pipeline": {"status": payloads["status/latest.json"]["status"], "recordCount": 1 if pipeline["run"] else 0},
        "market": {"status": market_status, "recordCount": len(market_records), "asOfDate": screening["as_of_date"]},
        "etf": {
            "status": _domain_status(len(normalized["etf"]["sources"]), len(etfs)),
            "recordCount": len(etfs),
            "asOfDate": normalized["etf"]["date"],
        },
        "financial": {
            "status": _domain_status(
                len(normalized["financial"]["sources"]),
                len(financial_reports) + len(financial_facts),
            ),
            "recordCount": len(financial_reports) + len(financial_facts),
            "asOfDate": normalized["financial"]["date"],
        },
        "news": {"status": _domain_status(len(normalized["news"]["sources"]), len(news)), "recordCount": len(news), "asOfDate": normalized["news"]["date"]},
        "events": {"status": _domain_status(len(normalized["events"]["sources"]), len(events)), "recordCount": len(events), "asOfDate": normalized["events"]["date"]},
        "reports": {"status": _domain_status(reports["source_count"], len(reports["reports"])), "recordCount": len(reports["reports"]), "asOfDate": reports["date"]},
    }
    statuses = {value["status"] for value in domain_summaries.values()}
    if "failed" in statuses:
        overall_status = "failed"
    elif all(status in {"ready", "empty"} for status in statuses):
        overall_status = "ready"
    else:
        overall_status = "partial"
    return payloads, {
        "generatedAt": generated_at,
        "status": overall_status,
        "datasets": domain_summaries,
    }


def _production_output_path(repository_root: Path, output_dir: Path) -> Tuple[Path, Path]:
    """Resolve the one permitted output without following any output symlink."""
    root_argument = Path(repository_root)
    if root_argument.is_symlink():
        raise ExportError("repository root must not be a symbolic link")
    root = root_argument.resolve()
    if not root.is_dir():
        raise ExportError("repository root does not exist or is not a directory")

    output_argument = Path(output_dir)
    if ".." in output_argument.parts:
        raise ExportError("output directory must not contain parent traversal")
    lexical_root = Path(os.path.abspath(root_argument))
    requested = (
        Path(os.path.abspath(output_argument))
        if output_argument.is_absolute()
        else Path(os.path.abspath(lexical_root / output_argument))
    )
    lexical_expected = lexical_root / "site/public/data"
    if requested != lexical_expected:
        raise ExportError("output directory must be repository site/public/data")
    expected = root / "site/public/data"

    current = root
    for part in ("site", "public", "data"):
        current = current / part
        if current.is_symlink():
            raise ExportError("output directory and its parents must not be symbolic links")
    if requested.resolve(strict=False) != expected:
        raise ExportError("output directory must be repository site/public/data")
    if expected.resolve(strict=False) != expected:
        raise ExportError("output directory must resolve inside the repository")
    return root, expected


def export_public_site(repository_root: Path, output_dir: Path) -> Path:
    """Verify sources and atomically replace only ``site/public/data``."""
    root, output = _production_output_path(repository_root, output_dir)

    payloads, index_metadata = build_public_payloads(root)
    encoded: Dict[str, bytes] = {}
    for path, payload in sorted(payloads.items()):
        _safe_public_payload(payload)
        content = _json_bytes(payload)
        if path.startswith("market/queue-") and len(content) >= MAX_QUEUE_SHARD_BYTES:
            raise ExportError("a market queue shard exceeds the 250 KiB publication limit")
        encoded[path] = content

    files = [
        {"path": path, "bytes": len(content), "sha256": _sha256(content)}
        for path, content in sorted(encoded.items())
    ]
    index_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": index_metadata["generatedAt"],
        "status": index_metadata["status"],
        "datasets": index_metadata["datasets"],
        "fileCount": len(files),
        "files": files,
    }
    _safe_public_payload(index_payload)
    encoded["index.json"] = _json_bytes(index_payload)

    _, rechecked_output = _production_output_path(root, output)
    if rechecked_output != output:
        raise ExportError("output directory changed before publication")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".public-site-data-", dir=output.parent))
    backup: Optional[Path] = None
    try:
        for relative, content in sorted(encoded.items()):
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        if output.exists() or output.is_symlink():
            backup = Path(tempfile.mkdtemp(prefix=".public-site-backup-", dir=output.parent))
            backup.rmdir()
            os.replace(output, backup)
        os.replace(stage, output)
        if backup is not None:
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()
    except Exception:
        if not output.exists() and backup is not None and backup.exists():
            os.replace(backup, output)
            backup = None
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if backup is not None and backup.exists():
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()
    return output


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export deterministic privacy-filtered data for GitHub Pages."
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="repository root (defaults to this script's repository)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("site/public/data"),
        help="output directory, relative to repository root by default",
    )
    args = parser.parse_args(argv)
    try:
        destination = export_public_site(args.repository_root, args.output_dir)
    except (ExportError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
