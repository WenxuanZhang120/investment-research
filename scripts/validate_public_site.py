#!/usr/bin/env python3
"""Fail closed before a generated static site is uploaded to GitHub Pages."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.public_payload_safety import (  # noqa: E402
    PublicPayloadSafetyError,
    assert_public_payload_safe,
)


DEFAULT_BUILD_DIR = REPOSITORY_ROOT / "site" / "dist"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024

# The output layout is intentionally smaller than a generic web allowlist.
# Any new public asset must be reviewed here before Pages can publish it.
ALLOWED_SUFFIXES = frozenset({".css", ".html", ".js", ".json", ".png"})
TEXT_SUFFIXES = frozenset({".css", ".html", ".js", ".json"})
FORBIDDEN_PATH_SEGMENTS = frozenset(
    {
        ".codex-collection-inbox",
        "codex-collection-inbox",
        "decision-journal",
        "decision_journal",
        "inbox",
        "portfolio",
        "private",
        "raw",
    }
)
LOCAL_PATH_PATTERNS = (
    ("macOS user path", re.compile(rb"(?<![A-Za-z0-9])/Users/")),
    ("Linux home path", re.compile(rb"(?<![A-Za-z0-9])/home/")),
    ("temporary path", re.compile(rb"(?<![A-Za-z0-9])/(?:private/)?tmp/")),
    ("mounted-volume path", re.compile(rb"(?<![A-Za-z0-9])/Volumes/")),
    (
        "macOS temporary path",
        re.compile(rb"(?<![A-Za-z0-9])/(?:private/)?var/folders/"),
    ),
    (
        "Windows user path",
        re.compile(rb"(?<![A-Za-z0-9])[A-Za-z]:[\\/]+Users[\\/]", re.IGNORECASE),
    ),
    ("file URI", re.compile(rb"(?i)(?<![A-Za-z0-9])file://")),
)
FORBIDDEN_CONTENT_PATH_PATTERNS = (
    re.compile(rb"(?i)(?<![A-Za-z0-9_.-])(?:data[\\/]+)?raw[\\/]+"),
    re.compile(rb"(?i)(?<![A-Za-z0-9_.-])private[\\/]+"),
    re.compile(rb"(?i)(?<![A-Za-z0-9_.-])portfolio[\\/]+"),
    re.compile(rb"(?i)(?<![A-Za-z0-9_.-])decision[_ -]journal[\\/]+"),
    re.compile(rb"(?i)(?<![A-Za-z0-9_.-])(?:\.codex-collection-)?inbox[\\/]+"),
)
SOURCE_MAP_REFERENCE_PATTERN = re.compile(rb"(?i)sourceMappingURL\s*=")
HTTP_URL_TOKEN_PATTERN = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ROOT_MANIFEST_FIELDS = frozenset(
    {"schemaVersion", "generatedAt", "status", "datasets", "fileCount", "files"}
)
ROOT_STATUS_VALUES = frozenset({"ready", "partial", "failed"})
DATASET_NAMES = frozenset(
    {"pipeline", "market", "etf", "financial", "news", "events", "reports"}
)
SHARD_NAMES = tuple("0123456789abcdef")
DOMAIN_STATUS_VALUES = frozenset({"ready", "empty", "missing"})
PIPELINE_STATUS_VALUES = frozenset({"ready", "partial", "failed", "missing"})
FIXED_DATA_PATHS = frozenset(
    {
        Path("data/status/latest.json"),
        Path("data/market/summary.json"),
        Path("data/etf/index.json"),
        Path("data/companies/index.json"),
        Path("data/content/index.json"),
        Path("data/provenance/index.json"),
        *(Path(f"data/market/queue-{shard}.json") for shard in SHARD_NAMES),
        *(Path(f"data/companies/details-{shard}.json") for shard in SHARD_NAMES),
    }
)
FIXED_JSON_PATHS = FIXED_DATA_PATHS | {Path("data/index.json")}
MARKET_RECORD_FIELDS = frozenset(
    {
        "asOfDate",
        "securityCode",
        "securityName",
        "eligible",
        "eligibilityReasons",
        "priority",
        "rank",
        "score",
        "scoreComponents",
        "marketCap",
        "peTtm",
        "netProfitMargin",
        "operatingCashFlowMargin",
        "financialPeriodEnd",
        "financialAvailableFrom",
    }
)
ETF_TEXT_FIELDS = frozenset(
    {
        "universeId",
        "etfCode",
        "etfName",
        "exchange",
        "asOfDate",
        "trackedIndex",
        "trackedIndexFamily",
        "fundType",
        "listingDate",
        "listingStatus",
    }
)
ETF_NUMBER_FIELDS = frozenset(
    {
        "price",
        "changePct",
        "volume",
        "turnover",
        "fundSize",
        "nav",
        "premiumDiscountRate",
        "managementFeeRate",
        "custodyFeeRate",
        "trackingError",
    }
)
CONTENT_FIELDS = frozenset(
    {
        "newsId",
        "eventId",
        "eventType",
        "publishedAt",
        "availableFrom",
        "publisher",
        "securityCode",
        "sourceSecurityCode",
        "securityName",
        "title",
        "summary",
        "url",
        "classificationKeywords",
    }
)
SECURITY_CODE_PATTERN = re.compile(r"^[0-9]{6}\.[A-Z]{2}$")


def _label(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return "<outside-build-directory>"


def _forbidden_path_category(relative_path: Path) -> str | None:
    for part in relative_path.parts:
        normalized = part.casefold().replace(" ", "_")
        if normalized in FORBIDDEN_PATH_SEGMENTS:
            return "private/raw/inbox path"
    return None


def _walk_without_links(root: Path, errors: list[str]) -> list[Path]:
    files: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            errors.append(f"{_label(directory, root)}: cannot read directory")
            return

        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                errors.append(f"{relative.as_posix()}: cannot inspect artifact")
                continue

            if stat.S_ISLNK(metadata.st_mode):
                errors.append(f"{relative.as_posix()}: symbolic links are forbidden")
                continue
            if _forbidden_path_category(relative) is not None:
                errors.append(
                    f"{relative.as_posix()}: private/raw/portfolio/decision_journal/inbox paths are forbidden"
                )
                continue
            if any(part.startswith(".") for part in relative.parts):
                errors.append(f"{relative.as_posix()}: hidden artifacts are forbidden")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                errors.append(f"{relative.as_posix()}: non-regular artifacts are forbidden")
                continue
            if metadata.st_nlink != 1:
                errors.append(f"{relative.as_posix()}: hard-linked artifacts are forbidden")
                continue
            files.append(path)

    visit(root)
    return files


def _strict_json_loads(payload: bytes) -> Any:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("JSON must be UTF-8") from error

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON contains a duplicate object key")
            result[key] = value
        return result

    def reject_non_finite(value: str) -> None:
        raise ValueError(f"JSON contains a non-finite number ({value})")

    return json.loads(
        text,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_non_finite,
    )


def _contains_forbidden_site_path(value: Any) -> bool:
    """Scan public values after removing complete HTTP(S) URL tokens."""
    if isinstance(value, dict):
        return any(_contains_forbidden_site_path(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_site_path(child) for child in value)
    if not isinstance(value, str):
        return False
    non_http_text = HTTP_URL_TOKEN_PATTERN.sub("", value).encode("utf-8")
    return any(
        pattern.search(non_http_text) for pattern in FORBIDDEN_CONTENT_PATH_PATTERNS
    )


def _read_and_validate_files(
    files: list[Path],
    *,
    root: Path,
    max_file_bytes: int,
    max_total_bytes: int,
    errors: list[str],
) -> tuple[dict[Path, bytes], dict[Path, Any]]:
    payloads: dict[Path, bytes] = {}
    json_documents: dict[Path, Any] = {}
    total_bytes = 0

    for path in files:
        relative = path.relative_to(root)
        label = relative.as_posix()
        suffix = path.suffix.casefold()
        if suffix == ".map":
            errors.append(f"{label}: source maps are forbidden")
            continue
        if suffix not in ALLOWED_SUFFIXES:
            errors.append(f"{label}: file type is not allowlisted")
            continue

        try:
            size = os.stat(path, follow_symlinks=False).st_size
        except OSError:
            errors.append(f"{label}: cannot inspect artifact size")
            continue
        total_bytes += size
        if size > max_file_bytes:
            errors.append(f"{label}: artifact exceeds the per-file size limit")
            continue

        try:
            payload = path.read_bytes()
        except OSError:
            errors.append(f"{label}: cannot read artifact")
            continue
        if len(payload) != size:
            errors.append(f"{label}: artifact changed while being validated")
            continue
        payloads[relative] = payload

        # JSON is checked structurally below. Text assets use the same shared
        # scanner, which removes complete HTTP(S) URL tokens before looking for
        # filesystem paths. Binary PNG metadata has no structured form, so it
        # retains a conservative byte-level scan.
        if suffix == ".png":
            for category, pattern in LOCAL_PATH_PATTERNS:
                if pattern.search(payload):
                    errors.append(f"{label}: contains a forbidden {category}")
                    break
            if any(
                pattern.search(payload) for pattern in FORBIDDEN_CONTENT_PATH_PATTERNS
            ):
                errors.append(f"{label}: contains a forbidden private repository path")
        if suffix in TEXT_SUFFIXES and SOURCE_MAP_REFERENCE_PATTERN.search(payload):
            errors.append(f"{label}: inline source maps are forbidden")

        if suffix in TEXT_SUFFIXES:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{label}: text artifact must be UTF-8")
                continue
            if suffix != ".json":
                safety_failed = False
                try:
                    # This also scans non-JSON assets for high-confidence Bearer
                    # values and local paths without exposing the matched text.
                    assert_public_payload_safe(text, location="$site")
                except PublicPayloadSafetyError:
                    errors.append(f"{label}: public payload safety check failed")
                    safety_failed = True
                if _contains_forbidden_site_path(text) and not safety_failed:
                    errors.append(f"{label}: public payload safety check failed")

        if suffix == ".json":
            try:
                document = _strict_json_loads(payload)
                assert_public_payload_safe(document, location="$site")
                if _contains_forbidden_site_path(document):
                    raise PublicPayloadSafetyError(
                        "public payload contains a forbidden site path"
                    )
            except (PublicPayloadSafetyError, TypeError, ValueError):
                errors.append(f"{label}: invalid or unsafe public JSON")
                continue
            json_documents[relative] = document

    if total_bytes > max_total_bytes:
        errors.append("static site exceeds the total size limit")
    return payloads, json_documents


def _manifest_path(value: Any) -> PurePosixPath | None:
    if not isinstance(value, str) or not value:
        return None
    if "\\" in value or value.startswith("/"):
        return None
    candidate = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    if candidate.as_posix() != value or candidate.suffix.casefold() != ".json":
        return None
    return candidate


def _valid_generated_at(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _valid_as_of_date(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _validate_root_manifest_contract(manifest: dict[str, Any], errors: list[str]) -> None:
    if set(manifest) != ROOT_MANIFEST_FIELDS:
        errors.append("data/index.json: root fields do not match schema version 1")

    schema_version = manifest.get("schemaVersion")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        errors.append("data/index.json: schemaVersion must be integer 1")

    if not _valid_generated_at(manifest.get("generatedAt")):
        errors.append(
            "data/index.json: generatedAt must be null or a timezone-aware ISO timestamp"
        )

    root_status = manifest.get("status")
    if not isinstance(root_status, str) or root_status not in ROOT_STATUS_VALUES:
        errors.append("data/index.json: status is not a supported root status")

    file_count = manifest.get("fileCount")
    entries = manifest.get("files")
    if (
        isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or file_count < 0
    ):
        errors.append("data/index.json: fileCount must be a non-negative integer")
    elif isinstance(entries, list) and file_count != len(entries):
        errors.append("data/index.json: fileCount does not match files")

    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict):
        errors.append("data/index.json: datasets must be an object")
        return
    if set(datasets) != DATASET_NAMES:
        errors.append("data/index.json: datasets do not match the required domains")

    dataset_statuses: list[str] = []
    for name in sorted(DATASET_NAMES):
        summary = datasets.get(name)
        if not isinstance(summary, dict):
            errors.append(f"data/index.json: datasets.{name} must be an object")
            continue
        expected_fields = (
            {"status", "recordCount"}
            if name == "pipeline"
            else {"status", "recordCount", "asOfDate"}
        )
        if set(summary) != expected_fields:
            errors.append(
                f"data/index.json: datasets.{name} fields do not match the contract"
            )
        status_value = summary.get("status")
        allowed_statuses = (
            PIPELINE_STATUS_VALUES if name == "pipeline" else DOMAIN_STATUS_VALUES
        )
        if not isinstance(status_value, str) or status_value not in allowed_statuses:
            errors.append(f"data/index.json: datasets.{name}.status is invalid")
        else:
            dataset_statuses.append(status_value)
        record_count = summary.get("recordCount")
        if (
            isinstance(record_count, bool)
            or not isinstance(record_count, int)
            or record_count < 0
        ):
            errors.append(
                f"data/index.json: datasets.{name}.recordCount must be non-negative"
            )
        if name != "pipeline" and not _valid_as_of_date(summary.get("asOfDate")):
            errors.append(f"data/index.json: datasets.{name}.asOfDate is invalid")

    if (
        len(dataset_statuses) == len(DATASET_NAMES)
        and isinstance(root_status, str)
        and root_status in ROOT_STATUS_VALUES
    ):
        if "failed" in dataset_statuses:
            expected_root_status = "failed"
        elif all(status in {"ready", "empty"} for status in dataset_statuses):
            expected_root_status = "ready"
        else:
            expected_root_status = "partial"
        if root_status != expected_root_status:
            errors.append("data/index.json: status does not match dataset statuses")


class _MetaPolicyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.csp_values: list[str] = []
        self.referrer_values: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() != "meta":
            return
        attributes = {
            name.casefold(): value for name, value in attrs if value is not None
        }
        if attributes.get("http-equiv", "").casefold() == "content-security-policy":
            self.csp_values.append(attributes.get("content", ""))
        if attributes.get("name", "").casefold() == "referrer":
            self.referrer_values.append(attributes.get("content", ""))


def _validate_index_security(payload: bytes | None, errors: list[str]) -> None:
    if not payload:
        return
    try:
        html = payload.decode("utf-8")
    except UnicodeDecodeError:
        return
    parser = _MetaPolicyParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        errors.append("index.html: cannot parse security metadata")
        return

    if len(parser.csp_values) != 1:
        errors.append("index.html: requires exactly one Content-Security-Policy meta")
    else:
        directives: dict[str, list[str]] = {}
        invalid = False
        for raw_directive in parser.csp_values[0].split(";"):
            tokens = raw_directive.strip().split()
            if not tokens:
                continue
            name = tokens[0].casefold()
            if name in directives:
                invalid = True
                continue
            directives[name] = [token.casefold() for token in tokens[1:]]
        required = {
            "default-src": ["'self'"],
            "base-uri": ["'self'"],
            "object-src": ["'none'"],
            "form-action": ["'none'"],
            "script-src": ["'self'"],
            "style-src": ["'self'"],
            "img-src": ["'self'", "data:"],
            "font-src": ["'self'"],
            "connect-src": ["'self'"],
        }
        if invalid or directives != required:
            errors.append(
                "index.html: Content-Security-Policy must keep the reviewed self-only contract"
            )

    if [value.casefold() for value in parser.referrer_values] != ["no-referrer"]:
        errors.append("index.html: requires exactly one no-referrer policy")


def _validate_artifact_layout(
    files: list[Path], *, root: Path, errors: list[str]
) -> None:
    relative_files = {path.relative_to(root) for path in files}
    required = FIXED_JSON_PATHS | {Path("index.html"), Path("og.png")}
    missing = sorted(required - relative_files, key=lambda value: value.as_posix())
    if missing:
        errors.append("static-site artifact is missing required fixed paths")

    for relative in sorted(relative_files, key=lambda value: value.as_posix()):
        if relative in required:
            continue
        if (
            len(relative.parts) == 2
            and relative.parts[0] == "assets"
            and relative.suffix.casefold() in {".js", ".css"}
            and relative.name not in {".js", ".css"}
        ):
            continue
        errors.append(f"{relative.as_posix()}: path is not in the fixed Pages artifact layout")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonnegative_int(value: Any) -> bool:
    return _is_int(value) and value >= 0


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_string_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _is_number_or_none(value: Any) -> bool:
    return value is None or _is_number(value)


def _exact_object(
    value: Any, *, fields: set[str] | frozenset[str], label: str, errors: list[str]
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{label}: must be an object")
        return None
    if set(value) != set(fields):
        errors.append(f"{label}: fields do not match the schema")
    return value


def _schema_version(document: dict[str, Any], label: str, errors: list[str]) -> None:
    if not _is_int(document.get("schemaVersion")) or document.get("schemaVersion") != 1:
        errors.append(f"{label}: schemaVersion must be integer 1")


def _status(
    document: dict[str, Any],
    *,
    allowed: frozenset[str],
    label: str,
    errors: list[str],
) -> str | None:
    value = document.get("status")
    if not isinstance(value, str) or value not in allowed:
        errors.append(f"{label}: status is invalid")
        return None
    return value


def _counted_list(
    document: dict[str, Any],
    *,
    collection: str,
    label: str,
    errors: list[str],
) -> list[Any]:
    count = document.get("recordCount")
    values = document.get(collection)
    if not _is_nonnegative_int(count):
        errors.append(f"{label}: recordCount must be a non-negative integer")
    if not isinstance(values, list):
        errors.append(f"{label}: {collection} must be a list")
        return []
    if _is_nonnegative_int(count) and count != len(values):
        errors.append(f"{label}: recordCount does not match {collection}")
    return values


def _status_for_count(status: str | None, count: int, label: str, errors: list[str]) -> None:
    if count > 0 and status != "ready":
        errors.append(f"{label}: non-empty records require ready status")
    if count == 0 and status == "ready":
        errors.append(f"{label}: ready status requires non-empty records")


def _string_list(value: Any, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append(f"{label}: must be a list of strings")
        return []
    return value


def _relative_public_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        return False
    candidate = PurePosixPath(value)
    return (
        candidate.as_posix() == value
        and all(part not in {"", ".", ".."} for part in candidate.parts)
    )


def _shard_for_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[0]


def _child_document(
    json_documents: dict[Path, Any], relative: str, errors: list[str]
) -> dict[str, Any] | None:
    path = Path("data") / relative
    document = json_documents.get(path)
    if not isinstance(document, dict):
        errors.append(f"{path.as_posix()}: missing or invalid object payload")
        return None
    return document


def _validate_market_record(
    value: Any, label: str, errors: list[str]
) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(value, dict):
        errors.append(f"{label}: market record must be an object")
        return None, None
    unknown = set(value) - MARKET_RECORD_FIELDS
    required = {
        "asOfDate",
        "securityCode",
        "securityName",
        "eligible",
        "eligibilityReasons",
        "priority",
        "rank",
        "score",
        "scoreComponents",
        "marketCap",
        "peTtm",
        "financialPeriodEnd",
        "financialAvailableFrom",
    }
    if unknown or not required.issubset(value):
        errors.append(f"{label}: market record fields do not match the contract")

    code = value.get("securityCode")
    if not isinstance(code, str) or not SECURITY_CODE_PATTERN.fullmatch(code):
        errors.append(f"{label}: securityCode is invalid")
        code = None
    if not _valid_as_of_date(value.get("asOfDate")) or value.get("asOfDate") is None:
        errors.append(f"{label}: asOfDate is invalid")
    if not _is_string_or_none(value.get("securityName")):
        errors.append(f"{label}: securityName must be text or null")
    if not isinstance(value.get("eligible"), bool):
        errors.append(f"{label}: eligible must be boolean")
    _string_list(value.get("eligibilityReasons"), f"{label}.eligibilityReasons", errors)
    if not isinstance(value.get("priority"), str) or not value.get("priority"):
        errors.append(f"{label}: priority must be non-empty text")
    rank = value.get("rank")
    if rank is not None and (not _is_int(rank) or rank < 1):
        errors.append(f"{label}: rank must be a positive integer or null")
    if not _is_number_or_none(value.get("score")):
        errors.append(f"{label}: score must be numeric or null")
    components = value.get("scoreComponents")
    if not isinstance(components, dict) or any(
        not isinstance(key, str) or not _is_number(component)
        for key, component in components.items()
    ):
        errors.append(f"{label}: scoreComponents must map text keys to numbers")
    for field in (
        "marketCap",
        "peTtm",
        "netProfitMargin",
        "operatingCashFlowMargin",
    ):
        if field in value and not _is_number_or_none(value.get(field)):
            errors.append(f"{label}: {field} must be numeric or null")
    for field in ("financialPeriodEnd", "financialAvailableFrom"):
        if not _valid_as_of_date(value.get(field)):
            errors.append(f"{label}: {field} is invalid")
    return code, value


def _validate_market_payloads(
    json_documents: dict[Path, Any], errors: list[str]
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    summary = _child_document(json_documents, "market/summary.json", errors)
    expected_summary_fields = {
        "schemaVersion",
        "status",
        "asOfDate",
        "bundleId",
        "screeningVersion",
        "purpose",
        "recordCount",
        "eligibleCount",
        "rejectCount",
        "priorityCounts",
        "shards",
    }
    summary_status: str | None = None
    descriptors: list[Any] = []
    if summary is not None:
        _exact_object(
            summary,
            fields=expected_summary_fields,
            label="data/market/summary.json",
            errors=errors,
        )
        _schema_version(summary, "data/market/summary.json", errors)
        summary_status = _status(
            summary,
            allowed=DOMAIN_STATUS_VALUES,
            label="data/market/summary.json",
            errors=errors,
        )
        for field in ("recordCount", "eligibleCount", "rejectCount"):
            if not _is_nonnegative_int(summary.get(field)):
                errors.append(
                    f"data/market/summary.json: {field} must be a non-negative integer"
                )
        if not _valid_as_of_date(summary.get("asOfDate")):
            errors.append("data/market/summary.json: asOfDate is invalid")
        for field in ("bundleId", "screeningVersion", "purpose"):
            if not _is_string_or_none(summary.get(field)):
                errors.append(f"data/market/summary.json: {field} must be text or null")
        counts = summary.get("priorityCounts")
        if not isinstance(counts, dict) or any(
            not isinstance(key, str) or not _is_nonnegative_int(count)
            for key, count in counts.items()
        ):
            errors.append(
                "data/market/summary.json: priorityCounts must map text keys to non-negative integers"
            )
        raw_descriptors = summary.get("shards")
        if not isinstance(raw_descriptors, list):
            errors.append("data/market/summary.json: shards must be a list")
        else:
            descriptors = raw_descriptors
            if len(descriptors) != len(SHARD_NAMES):
                errors.append("data/market/summary.json: exactly 16 shards are required")

    all_records: dict[str, dict[str, Any]] = {}
    actual_counts: dict[str, int] = {}
    actual_priorities: dict[str, int] = {}
    eligible_count = 0
    for shard in SHARD_NAMES:
        relative = f"market/queue-{shard}.json"
        label = f"data/{relative}"
        document = _child_document(json_documents, relative, errors)
        if document is None:
            continue
        _exact_object(
            document,
            fields={"schemaVersion", "status", "shard", "recordCount", "records"},
            label=label,
            errors=errors,
        )
        _schema_version(document, label, errors)
        shard_status = _status(
            document, allowed=DOMAIN_STATUS_VALUES, label=label, errors=errors
        )
        if shard_status is not None and summary_status is not None and shard_status != summary_status:
            errors.append(f"{label}: status does not match market summary")
        if document.get("shard") != shard:
            errors.append(f"{label}: shard name does not match its path")
        records = _counted_list(
            document, collection="records", label=label, errors=errors
        )
        actual_counts[shard] = len(records)
        for index, record in enumerate(records):
            record_label = f"{label}: records[{index}]"
            code, validated = _validate_market_record(record, record_label, errors)
            if code is None or validated is None:
                continue
            if _shard_for_key(code) != shard:
                errors.append(f"{record_label}: securityCode is in the wrong shard")
            if code in all_records:
                errors.append(f"{record_label}: duplicate securityCode")
            else:
                all_records[code] = validated
            priority = validated.get("priority")
            if isinstance(priority, str):
                actual_priorities[priority] = actual_priorities.get(priority, 0) + 1
            if validated.get("eligible") is True:
                eligible_count += 1

    if summary is not None:
        if _is_nonnegative_int(summary.get("recordCount")):
            if summary["recordCount"] != sum(actual_counts.values()):
                errors.append("data/market/summary.json: recordCount does not match shards")
            _status_for_count(
                summary_status,
                summary["recordCount"],
                "data/market/summary.json",
                errors,
            )
        if summary.get("eligibleCount") != eligible_count:
            errors.append("data/market/summary.json: eligibleCount does not match records")
        if _is_nonnegative_int(summary.get("recordCount")) and _is_nonnegative_int(
            summary.get("rejectCount")
        ):
            if summary["rejectCount"] != summary["recordCount"] - eligible_count:
                errors.append("data/market/summary.json: rejectCount does not match records")
        if isinstance(summary.get("priorityCounts"), dict) and summary.get(
            "priorityCounts"
        ) != dict(sorted(actual_priorities.items())):
            errors.append("data/market/summary.json: priorityCounts do not match records")
        for index, shard in enumerate(SHARD_NAMES):
            if index >= len(descriptors):
                break
            descriptor = descriptors[index]
            expected = {
                "name": shard,
                "path": f"market/queue-{shard}.json",
                "recordCount": actual_counts.get(shard, 0),
            }
            if not isinstance(descriptor, dict) or descriptor != expected:
                errors.append(
                    f"data/market/summary.json: shards[{index}] does not match shard {shard}"
                )
    return summary, all_records


def _validate_financial_report(
    value: Any, label: str, company_code: str, errors: list[str]
) -> None:
    fields = {
        "securityCode",
        "securityName",
        "periodEnd",
        "reportType",
        "reportPeriodLabel",
        "filingDate",
        "availableFrom",
        "factCount",
        "presentFactCount",
        "missingFactCount",
    }
    document = _exact_object(value, fields=fields, label=label, errors=errors)
    if document is None:
        return
    if document.get("securityCode") != company_code:
        errors.append(f"{label}: securityCode does not match its company")
    if not _is_string_or_none(document.get("securityName")):
        errors.append(f"{label}: securityName must be text or null")
    for field in (
        "periodEnd",
        "reportType",
        "reportPeriodLabel",
        "filingDate",
        "availableFrom",
    ):
        if not isinstance(document.get(field), str) or not document.get(field):
            errors.append(f"{label}: {field} must be non-empty text")
    for field in ("factCount", "presentFactCount", "missingFactCount"):
        if not _is_nonnegative_int(document.get(field)):
            errors.append(f"{label}: {field} must be a non-negative integer")
    if all(
        _is_nonnegative_int(document.get(field))
        for field in ("factCount", "presentFactCount", "missingFactCount")
    ) and document["factCount"] != document["presentFactCount"] + document["missingFactCount"]:
        errors.append(f"{label}: financial fact counts are inconsistent")


def _validate_financial_fact(
    value: Any, label: str, company_code: str, errors: list[str]
) -> None:
    fields = {
        "securityCode",
        "securityName",
        "periodEnd",
        "reportType",
        "reportPeriodLabel",
        "filingDate",
        "availableFrom",
        "canonicalFieldName",
        "statementType",
        "value",
        "unit",
        "valueNature",
        "valueStatus",
    }
    document = _exact_object(value, fields=fields, label=label, errors=errors)
    if document is None:
        return
    if document.get("securityCode") != company_code:
        errors.append(f"{label}: securityCode does not match its company")
    if not _is_string_or_none(document.get("securityName")):
        errors.append(f"{label}: securityName must be text or null")
    for field in (
        "periodEnd",
        "reportType",
        "reportPeriodLabel",
        "filingDate",
        "availableFrom",
        "canonicalFieldName",
        "statementType",
        "valueNature",
        "valueStatus",
    ):
        if not isinstance(document.get(field), str) or not document.get(field):
            errors.append(f"{label}: {field} must be non-empty text")
    value_field = document.get("value")
    if value_field is not None and not isinstance(value_field, str) and not _is_number(
        value_field
    ):
        errors.append(f"{label}: value must be text, numeric, or null")
    if not _is_string_or_none(document.get("unit")):
        errors.append(f"{label}: unit must be text or null")


def _validate_company_payloads(
    json_documents: dict[Path, Any],
    market_records: dict[str, dict[str, Any]],
    errors: list[str],
) -> tuple[dict[str, Any] | None, int, int]:
    index_document = _child_document(json_documents, "companies/index.json", errors)
    index_status: str | None = None
    summaries: list[Any] = []
    if index_document is not None:
        _exact_object(
            index_document,
            fields={"schemaVersion", "status", "recordCount", "companies"},
            label="data/companies/index.json",
            errors=errors,
        )
        _schema_version(index_document, "data/companies/index.json", errors)
        index_status = _status(
            index_document,
            allowed=DOMAIN_STATUS_VALUES,
            label="data/companies/index.json",
            errors=errors,
        )
        summaries = _counted_list(
            index_document,
            collection="companies",
            label="data/companies/index.json",
            errors=errors,
        )
        _status_for_count(
            index_status,
            len(summaries),
            "data/companies/index.json",
            errors,
        )

    summary_by_code: dict[str, dict[str, Any]] = {}
    summary_fields = {
        "securityCode",
        "securityName",
        "priority",
        "rank",
        "hasMarket",
        "financialReportCount",
        "financialFactCount",
        "newsCount",
        "eventCount",
        "detailShard",
    }
    for position, value in enumerate(summaries):
        label = f"data/companies/index.json: companies[{position}]"
        summary = _exact_object(value, fields=summary_fields, label=label, errors=errors)
        if summary is None:
            continue
        code = summary.get("securityCode")
        if not isinstance(code, str) or not SECURITY_CODE_PATTERN.fullmatch(code):
            errors.append(f"{label}: securityCode is invalid")
            continue
        if code in summary_by_code:
            errors.append(f"{label}: duplicate securityCode")
        else:
            summary_by_code[code] = summary
        if not _is_string_or_none(summary.get("securityName")):
            errors.append(f"{label}: securityName must be text or null")
        if not _is_string_or_none(summary.get("priority")):
            errors.append(f"{label}: priority must be text or null")
        rank = summary.get("rank")
        if rank is not None and (not _is_int(rank) or rank < 1):
            errors.append(f"{label}: rank must be a positive integer or null")
        if not isinstance(summary.get("hasMarket"), bool):
            errors.append(f"{label}: hasMarket must be boolean")
        for field in (
            "financialReportCount",
            "financialFactCount",
            "newsCount",
            "eventCount",
        ):
            if not _is_nonnegative_int(summary.get(field)):
                errors.append(f"{label}: {field} must be a non-negative integer")
        if summary.get("detailShard") != _shard_for_key(code):
            errors.append(f"{label}: detailShard does not match securityCode")

    details_by_code: dict[str, dict[str, Any]] = {}
    financial_report_count = 0
    financial_fact_count = 0
    detail_fields = {
        "securityCode",
        "securityName",
        "market",
        "financialReports",
        "financialFacts",
        "newsIds",
        "eventIds",
    }
    for shard in SHARD_NAMES:
        relative = f"companies/details-{shard}.json"
        label = f"data/{relative}"
        document = _child_document(json_documents, relative, errors)
        if document is None:
            continue
        _exact_object(
            document,
            fields={"schemaVersion", "status", "shard", "recordCount", "companies"},
            label=label,
            errors=errors,
        )
        _schema_version(document, label, errors)
        detail_status = _status(
            document, allowed=DOMAIN_STATUS_VALUES, label=label, errors=errors
        )
        if detail_status is not None and index_status is not None and detail_status != index_status:
            errors.append(f"{label}: status does not match companies index")
        if document.get("shard") != shard:
            errors.append(f"{label}: shard name does not match its path")
        details = _counted_list(
            document, collection="companies", label=label, errors=errors
        )
        for position, value in enumerate(details):
            detail_label = f"{label}: companies[{position}]"
            detail = _exact_object(
                value, fields=detail_fields, label=detail_label, errors=errors
            )
            if detail is None:
                continue
            code = detail.get("securityCode")
            if not isinstance(code, str) or not SECURITY_CODE_PATTERN.fullmatch(code):
                errors.append(f"{detail_label}: securityCode is invalid")
                continue
            if _shard_for_key(code) != shard:
                errors.append(f"{detail_label}: securityCode is in the wrong shard")
            if code in details_by_code:
                errors.append(f"{detail_label}: duplicate securityCode")
            else:
                details_by_code[code] = detail
            if not _is_string_or_none(detail.get("securityName")):
                errors.append(f"{detail_label}: securityName must be text or null")
            market = detail.get("market")
            if market is not None:
                market_code, _ = _validate_market_record(
                    market, f"{detail_label}.market", errors
                )
                if market_code != code:
                    errors.append(f"{detail_label}: market securityCode does not match")
                if market_records.get(code) != market:
                    errors.append(f"{detail_label}: market record does not match market shards")
            reports = detail.get("financialReports")
            if not isinstance(reports, list):
                errors.append(f"{detail_label}: financialReports must be a list")
                reports = []
            facts = detail.get("financialFacts")
            if not isinstance(facts, list):
                errors.append(f"{detail_label}: financialFacts must be a list")
                facts = []
            financial_report_count += len(reports)
            financial_fact_count += len(facts)
            for index, report in enumerate(reports):
                _validate_financial_report(
                    report, f"{detail_label}.financialReports[{index}]", code, errors
                )
            for index, fact in enumerate(facts):
                _validate_financial_fact(
                    fact, f"{detail_label}.financialFacts[{index}]", code, errors
                )
            for collection in ("newsIds", "eventIds"):
                identifiers = _string_list(
                    detail.get(collection), f"{detail_label}.{collection}", errors
                )
                if len(set(identifiers)) != len(identifiers):
                    errors.append(f"{detail_label}: {collection} contains duplicates")

    if set(summary_by_code) != set(details_by_code):
        errors.append("data/companies/index.json: company codes do not match detail shards")
    if index_document is not None and _is_nonnegative_int(index_document.get("recordCount")):
        if index_document["recordCount"] != len(details_by_code):
            errors.append("data/companies/index.json: recordCount does not match detail shards")
    for code in sorted(set(summary_by_code) & set(details_by_code)):
        summary = summary_by_code[code]
        detail = details_by_code[code]
        checks = {
            "hasMarket": detail.get("market") is not None,
            "financialReportCount": len(detail.get("financialReports", []))
            if isinstance(detail.get("financialReports"), list)
            else 0,
            "financialFactCount": len(detail.get("financialFacts", []))
            if isinstance(detail.get("financialFacts"), list)
            else 0,
            "newsCount": len(detail.get("newsIds", []))
            if isinstance(detail.get("newsIds"), list)
            else 0,
            "eventCount": len(detail.get("eventIds", []))
            if isinstance(detail.get("eventIds"), list)
            else 0,
        }
        if any(summary.get(field) != expected for field, expected in checks.items()):
            errors.append(
                "data/companies/index.json: company summary counts do not match detail shards"
            )
        market = detail.get("market")
        expected_priority = market.get("priority") if isinstance(market, dict) else None
        expected_rank = market.get("rank") if isinstance(market, dict) else None
        if summary.get("priority") != expected_priority or summary.get("rank") != expected_rank:
            errors.append(
                "data/companies/index.json: company market summary does not match detail"
            )
    if not set(market_records).issubset(details_by_code):
        errors.append("data/companies: market records are not fully covered by company details")
    return index_document, financial_report_count, financial_fact_count


def _validate_etf_payload(
    json_documents: dict[Path, Any], errors: list[str]
) -> dict[str, Any] | None:
    label = "data/etf/index.json"
    document = _child_document(json_documents, "etf/index.json", errors)
    if document is None:
        return None
    _exact_object(
        document,
        fields={"schemaVersion", "status", "asOfDate", "recordCount", "records"},
        label=label,
        errors=errors,
    )
    _schema_version(document, label, errors)
    status_value = _status(
        document, allowed=DOMAIN_STATUS_VALUES, label=label, errors=errors
    )
    if not _valid_as_of_date(document.get("asOfDate")):
        errors.append(f"{label}: asOfDate is invalid")
    records = _counted_list(document, collection="records", label=label, errors=errors)
    _status_for_count(status_value, len(records), label, errors)
    seen: set[tuple[str, str]] = set()
    allowed_fields = ETF_TEXT_FIELDS | ETF_NUMBER_FIELDS | {"fundTypeMemberships"}
    for index, value in enumerate(records):
        record_label = f"{label}: records[{index}]"
        if not isinstance(value, dict):
            errors.append(f"{record_label}: ETF record must be an object")
            continue
        if set(value) - allowed_fields or not {"etfCode", "asOfDate"}.issubset(value):
            errors.append(f"{record_label}: ETF fields do not match the contract")
        code = value.get("etfCode")
        as_of_date = value.get("asOfDate")
        if not isinstance(code, str) or not code:
            errors.append(f"{record_label}: etfCode must be non-empty text")
        if not _valid_as_of_date(as_of_date) or as_of_date is None:
            errors.append(f"{record_label}: asOfDate is invalid")
        if isinstance(code, str) and isinstance(as_of_date, str):
            key = (code, as_of_date)
            if key in seen:
                errors.append(f"{record_label}: duplicate etfCode/asOfDate")
            seen.add(key)
        for field in ETF_TEXT_FIELDS:
            if field in value and not _is_string_or_none(value[field]):
                errors.append(f"{record_label}: {field} must be text or null")
        for field in ETF_NUMBER_FIELDS:
            if field in value and not _is_number_or_none(value[field]):
                errors.append(f"{record_label}: {field} must be numeric or null")
        if "fundTypeMemberships" in value:
            _string_list(
                value["fundTypeMemberships"],
                f"{record_label}.fundTypeMemberships",
                errors,
            )
    return document


def _validate_content_record(
    value: Any, *, kind: str, label: str, errors: list[str]
) -> str | None:
    if not isinstance(value, dict):
        errors.append(f"{label}: content record must be an object")
        return None
    if set(value) - CONTENT_FIELDS:
        errors.append(f"{label}: content record contains unknown fields")
    identifier_field = "newsId" if kind == "news" else "eventId"
    identifier = value.get(identifier_field)
    if not isinstance(identifier, str) or not identifier:
        errors.append(f"{label}: {identifier_field} must be non-empty text")
        identifier = None
    for field in (
        "eventType",
        "publishedAt",
        "availableFrom",
        "publisher",
        "securityCode",
        "sourceSecurityCode",
        "securityName",
        "title",
        "summary",
        "url",
    ):
        if field in value and not _is_string_or_none(value[field]):
            errors.append(f"{label}: {field} must be text or null")
    if "classificationKeywords" in value:
        _string_list(
            value["classificationKeywords"],
            f"{label}.classificationKeywords",
            errors,
        )
    url = value.get("url")
    if url is not None and (
        not isinstance(url, str)
        or not re.match(r"^https?://[^\s]+$", url, flags=re.IGNORECASE)
    ):
        errors.append(f"{label}: url must be an absolute HTTP or HTTPS URL")
    return identifier


def _validate_content_payload(
    json_documents: dict[Path, Any], errors: list[str]
) -> tuple[dict[str, Any] | None, set[str], set[str]]:
    label = "data/content/index.json"
    document = _child_document(json_documents, "content/index.json", errors)
    if document is None:
        return None, set(), set()
    _exact_object(
        document,
        fields={"schemaVersion", "status", "domains", "news", "events", "reports"},
        label=label,
        errors=errors,
    )
    _schema_version(document, label, errors)
    overall_status = _status(
        document, allowed=DOMAIN_STATUS_VALUES, label=label, errors=errors
    )
    domains = document.get("domains")
    if not isinstance(domains, dict) or set(domains) != {"news", "events", "reports"}:
        errors.append(f"{label}: domains must contain exactly news, events, and reports")
        domains = {}

    collections: dict[str, list[Any]] = {}
    domain_statuses: list[str] = []
    for domain in ("news", "events", "reports"):
        values = document.get(domain)
        if not isinstance(values, list):
            errors.append(f"{label}: {domain} must be a list")
            values = []
        collections[domain] = values
        summary = domains.get(domain)
        summary_label = f"{label}: domains.{domain}"
        summary = _exact_object(
            summary,
            fields={"status", "asOfDate", "recordCount"},
            label=summary_label,
            errors=errors,
        )
        if summary is None:
            continue
        status_value = _status(
            summary,
            allowed=DOMAIN_STATUS_VALUES,
            label=summary_label,
            errors=errors,
        )
        if status_value is not None:
            domain_statuses.append(status_value)
        if not _valid_as_of_date(summary.get("asOfDate")):
            errors.append(f"{summary_label}: asOfDate is invalid")
        if not _is_nonnegative_int(summary.get("recordCount")):
            errors.append(f"{summary_label}: recordCount must be non-negative")
        elif summary["recordCount"] != len(values):
            errors.append(f"{summary_label}: recordCount does not match {domain}")
        _status_for_count(status_value, len(values), summary_label, errors)

    total = sum(len(values) for values in collections.values())
    if total > 0:
        expected_overall = "ready"
    elif domain_statuses and all(status == "missing" for status in domain_statuses):
        expected_overall = "missing"
    else:
        expected_overall = "empty"
    if len(domain_statuses) == 3 and overall_status != expected_overall:
        errors.append(f"{label}: status does not match domain statuses and counts")

    news_ids: set[str] = set()
    event_ids: set[str] = set()
    for kind, target in (("news", news_ids), ("events", event_ids)):
        for index, value in enumerate(collections[kind]):
            identifier = _validate_content_record(
                value,
                kind=kind,
                label=f"{label}: {kind}[{index}]",
                errors=errors,
            )
            if identifier is not None:
                if identifier in target:
                    errors.append(f"{label}: duplicate {kind} identifier")
                target.add(identifier)

    report_fields = {
        "kind",
        "title",
        "asOfDate",
        "path",
        "sha256",
        "bytes",
        "recordCount",
        "status",
    }
    for index, value in enumerate(collections["reports"]):
        report_label = f"{label}: reports[{index}]"
        report = _exact_object(
            value, fields=report_fields, label=report_label, errors=errors
        )
        if report is None:
            continue
        for field in ("kind", "title"):
            if not isinstance(report.get(field), str) or not report.get(field):
                errors.append(f"{report_label}: {field} must be non-empty text")
        if not _valid_as_of_date(report.get("asOfDate")):
            errors.append(f"{report_label}: asOfDate is invalid")
        if not _relative_public_path(report.get("path")):
            errors.append(f"{report_label}: path must be repository-relative")
        if not isinstance(report.get("sha256"), str) or not SHA256_PATTERN.fullmatch(
            report["sha256"]
        ):
            errors.append(f"{report_label}: sha256 is invalid")
        if not _is_nonnegative_int(report.get("bytes")):
            errors.append(f"{report_label}: bytes must be a non-negative integer")
        record_count = report.get("recordCount")
        if record_count is not None and not _is_nonnegative_int(record_count):
            errors.append(f"{report_label}: recordCount must be non-negative or null")
        report_status = report.get("status")
        if not isinstance(report_status, str) or report_status not in DOMAIN_STATUS_VALUES:
            errors.append(f"{report_label}: status is invalid")
    return document, news_ids, event_ids


def _validate_provenance_payload(
    json_documents: dict[Path, Any], errors: list[str]
) -> dict[str, Any] | None:
    label = "data/provenance/index.json"
    document = _child_document(json_documents, "provenance/index.json", errors)
    if document is None:
        return None
    _exact_object(
        document,
        fields={"schemaVersion", "status", "generatedAt", "sourceCount", "sources"},
        label=label,
        errors=errors,
    )
    _schema_version(document, label, errors)
    status_value = _status(
        document,
        allowed=frozenset({"ready", "missing"}),
        label=label,
        errors=errors,
    )
    if not _valid_generated_at(document.get("generatedAt")):
        errors.append(f"{label}: generatedAt must be null or a timezone-aware timestamp")
    sources = document.get("sources")
    if not isinstance(sources, list):
        errors.append(f"{label}: sources must be a list")
        sources = []
    if not _is_nonnegative_int(document.get("sourceCount")):
        errors.append(f"{label}: sourceCount must be a non-negative integer")
    elif document["sourceCount"] != len(sources):
        errors.append(f"{label}: sourceCount does not match sources")
    if (len(sources) > 0 and status_value != "ready") or (
        len(sources) == 0 and status_value != "missing"
    ):
        errors.append(f"{label}: status does not match sourceCount")

    source_fields = {
        "domain",
        "path",
        "sha256",
        "bytes",
        "bundleId",
        "runId",
        "asOfDate",
        "fetchedAt",
        "recordCount",
    }
    seen: set[tuple[str, str]] = set()
    for index, value in enumerate(sources):
        source_label = f"{label}: sources[{index}]"
        source = _exact_object(
            value, fields=source_fields, label=source_label, errors=errors
        )
        if source is None:
            continue
        domain = source.get("domain")
        path = source.get("path")
        if not isinstance(domain, str) or not domain:
            errors.append(f"{source_label}: domain must be non-empty text")
        if not _relative_public_path(path):
            errors.append(f"{source_label}: path must be repository-relative")
        if isinstance(domain, str) and isinstance(path, str):
            key = (domain, path)
            if key in seen:
                errors.append(f"{source_label}: duplicate provenance source")
            seen.add(key)
        if not isinstance(source.get("sha256"), str) or not SHA256_PATTERN.fullmatch(
            source["sha256"]
        ):
            errors.append(f"{source_label}: sha256 is invalid")
        if not _is_nonnegative_int(source.get("bytes")):
            errors.append(f"{source_label}: bytes must be a non-negative integer")
        for field in ("bundleId", "runId"):
            if not _is_string_or_none(source.get(field)):
                errors.append(f"{source_label}: {field} must be text or null")
        if not _valid_as_of_date(source.get("asOfDate")):
            errors.append(f"{source_label}: asOfDate is invalid")
        fetched_at = source.get("fetchedAt")
        if fetched_at is not None and not _valid_generated_at(fetched_at):
            errors.append(f"{source_label}: fetchedAt is invalid")
        record_count = source.get("recordCount")
        if record_count is not None and not _is_nonnegative_int(record_count):
            errors.append(f"{source_label}: recordCount must be non-negative or null")
    return document


def _validate_pipeline_payload(
    json_documents: dict[Path, Any], errors: list[str]
) -> dict[str, Any] | None:
    label = "data/status/latest.json"
    document = _child_document(json_documents, "status/latest.json", errors)
    if document is None:
        return None
    _exact_object(
        document,
        fields={"schemaVersion", "status", "artifactAvailable", "run"},
        label=label,
        errors=errors,
    )
    _schema_version(document, label, errors)
    status_value = _status(
        document, allowed=PIPELINE_STATUS_VALUES, label=label, errors=errors
    )
    available = document.get("artifactAvailable")
    if not isinstance(available, bool):
        errors.append(f"{label}: artifactAvailable must be boolean")
    run = document.get("run")
    if available is False:
        if run is not None or status_value != "missing":
            errors.append(f"{label}: unavailable artifact must have missing status and null run")
        return document
    if available is not True or not isinstance(run, dict):
        errors.append(f"{label}: available artifact requires an object run")
        return document
    if status_value == "missing":
        errors.append(f"{label}: available artifact cannot have missing status")

    run_fields = {
        "runId",
        "pipelineVersion",
        "status",
        "startedAt",
        "finishedAt",
        "stepCount",
        "steps",
        "readiness",
    }
    _exact_object(run, fields=run_fields, label=f"{label}: run", errors=errors)
    for field in ("runId", "pipelineVersion", "status"):
        if not _is_string_or_none(run.get(field)):
            errors.append(f"{label}: run.{field} must be text or null")
    for field in ("startedAt", "finishedAt"):
        value = run.get(field)
        if value is not None and not _valid_generated_at(value):
            errors.append(f"{label}: run.{field} is invalid")
    steps = run.get("steps")
    if not isinstance(steps, list):
        errors.append(f"{label}: run.steps must be a list")
        steps = []
    if not _is_nonnegative_int(run.get("stepCount")):
        errors.append(f"{label}: run.stepCount must be a non-negative integer")
    elif run["stepCount"] != len(steps):
        errors.append(f"{label}: run.stepCount does not match steps")
    step_fields = {
        "stage",
        "stepId",
        "status",
        "exitCode",
        "errorType",
        "startedAt",
        "finishedAt",
    }
    for index, value in enumerate(steps):
        step_label = f"{label}: run.steps[{index}]"
        step = _exact_object(value, fields=step_fields, label=step_label, errors=errors)
        if step is None:
            continue
        for field in ("stage", "stepId", "status", "errorType"):
            if not _is_string_or_none(step.get(field)):
                errors.append(f"{step_label}: {field} must be text or null")
        exit_code = step.get("exitCode")
        if exit_code is not None and not _is_int(exit_code):
            errors.append(f"{step_label}: exitCode must be integer or null")
        for field in ("startedAt", "finishedAt"):
            value = step.get(field)
            if value is not None and not _valid_generated_at(value):
                errors.append(f"{step_label}: {field} is invalid")

    readiness_fields = {
        "status",
        "plannedStepCount",
        "incompleteJobCount",
        "researchStatus",
        "screeningStatus",
        "monitoringMatchedSnapshotCount",
    }
    readiness = _exact_object(
        run.get("readiness"),
        fields=readiness_fields,
        label=f"{label}: run.readiness",
        errors=errors,
    )
    if readiness is not None:
        for field in ("status", "researchStatus", "screeningStatus"):
            if not _is_string_or_none(readiness.get(field)):
                errors.append(f"{label}: run.readiness.{field} must be text or null")
        for field in (
            "plannedStepCount",
            "incompleteJobCount",
            "monitoringMatchedSnapshotCount",
        ):
            value = readiness.get(field)
            if value is not None and not _is_nonnegative_int(value):
                errors.append(
                    f"{label}: run.readiness.{field} must be non-negative or null"
                )
    return document


def _dataset_summary(
    manifest: dict[str, Any] | None, domain: str
) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        return None
    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict):
        return None
    summary = datasets.get(domain)
    return summary if isinstance(summary, dict) else None


def _compare_dataset(
    manifest: dict[str, Any] | None,
    *,
    domain: str,
    status: Any,
    record_count: int,
    as_of_date: Any = None,
    errors: list[str],
) -> None:
    summary = _dataset_summary(manifest, domain)
    if summary is None:
        return
    expected = {"status": status, "recordCount": record_count}
    if domain != "pipeline":
        expected["asOfDate"] = as_of_date
    if summary != expected:
        errors.append(f"data/index.json: datasets.{domain} does not match its payload")


def _validate_fixed_payload_contracts(
    manifest: dict[str, Any] | None,
    json_documents: dict[Path, Any],
    errors: list[str],
) -> None:
    pipeline = _validate_pipeline_payload(json_documents, errors)
    market, market_records = _validate_market_payloads(json_documents, errors)
    companies, report_count, fact_count = _validate_company_payloads(
        json_documents, market_records, errors
    )
    etf = _validate_etf_payload(json_documents, errors)
    content, news_ids, event_ids = _validate_content_payload(json_documents, errors)
    provenance = _validate_provenance_payload(json_documents, errors)

    for shard in SHARD_NAMES:
        detail = json_documents.get(Path(f"data/companies/details-{shard}.json"))
        if not isinstance(detail, dict) or not isinstance(detail.get("companies"), list):
            continue
        for position, company in enumerate(detail["companies"]):
            if not isinstance(company, dict):
                continue
            company_news = company.get("newsIds")
            company_events = company.get("eventIds")
            if isinstance(company_news, list) and not set(company_news).issubset(news_ids):
                errors.append(
                    f"data/companies/details-{shard}.json: companies[{position}] references unknown news"
                )
            if isinstance(company_events, list) and not set(company_events).issubset(event_ids):
                errors.append(
                    f"data/companies/details-{shard}.json: companies[{position}] references unknown events"
                )

    pipeline_count = 1 if isinstance(pipeline, dict) and pipeline.get("run") is not None else 0
    if pipeline is not None:
        _compare_dataset(
            manifest,
            domain="pipeline",
            status=pipeline.get("status"),
            record_count=pipeline_count,
            errors=errors,
        )
    if market is not None:
        _compare_dataset(
            manifest,
            domain="market",
            status=market.get("status"),
            record_count=len(market_records),
            as_of_date=market.get("asOfDate"),
            errors=errors,
        )
    if etf is not None and isinstance(etf.get("records"), list):
        _compare_dataset(
            manifest,
            domain="etf",
            status=etf.get("status"),
            record_count=len(etf["records"]),
            as_of_date=etf.get("asOfDate"),
            errors=errors,
        )
    if content is not None and isinstance(content.get("domains"), dict):
        for domain in ("news", "events", "reports"):
            summary = content["domains"].get(domain)
            values = content.get(domain)
            if isinstance(summary, dict) and isinstance(values, list):
                _compare_dataset(
                    manifest,
                    domain=domain,
                    status=summary.get("status"),
                    record_count=len(values),
                    as_of_date=summary.get("asOfDate"),
                    errors=errors,
                )

    financial_summary = _dataset_summary(manifest, "financial")
    if financial_summary is not None:
        financial_total = report_count + fact_count
        if financial_summary.get("recordCount") != financial_total:
            errors.append(
                "data/index.json: datasets.financial recordCount does not match company details"
            )
        provenance_sources = (
            provenance.get("sources")
            if isinstance(provenance, dict) and isinstance(provenance.get("sources"), list)
            else []
        )
        has_financial_source = any(
            isinstance(source, dict) and source.get("domain") == "financial"
            for source in provenance_sources
        )
        expected_financial_status = (
            "ready"
            if financial_total > 0
            else "empty"
            if has_financial_source
            else "missing"
        )
        if financial_summary.get("status") != expected_financial_status:
            errors.append(
                "data/index.json: datasets.financial status does not match public sources"
            )

    if isinstance(manifest, dict) and isinstance(provenance, dict):
        if manifest.get("generatedAt") != provenance.get("generatedAt"):
            errors.append("data/index.json: generatedAt does not match provenance")

    if companies is not None and market is not None:
        provenance_sources = (
            provenance.get("sources")
            if isinstance(provenance, dict) and isinstance(provenance.get("sources"), list)
            else []
        )
        has_company_source = any(
            isinstance(source, dict)
            and source.get("domain") in {"screening", "financial"}
            for source in provenance_sources
        )
        company_count = (
            len(companies.get("companies", []))
            if isinstance(companies.get("companies"), list)
            else 0
        )
        expected_company_status = (
            "ready"
            if company_count > 0
            else "empty"
            if has_company_source
            else "missing"
        )
        if companies.get("status") != expected_company_status:
            errors.append("data/companies/index.json: status does not match public sources")


def _validate_manifest(
    *,
    root: Path,
    payloads: dict[Path, bytes],
    json_documents: dict[Path, Any],
    errors: list[str],
) -> dict[str, Any] | None:
    manifest_relative = Path("data/index.json")
    manifest = json_documents.get(manifest_relative)
    if not isinstance(manifest, dict):
        errors.append("data/index.json: missing or invalid JSON manifest")
        return None

    _validate_root_manifest_contract(manifest, errors)

    entries = manifest.get("files")
    if not isinstance(entries, list):
        errors.append("data/index.json: files must be a list")
        return manifest

    declared: set[Path] = set()
    for index, entry in enumerate(entries):
        entry_label = f"data/index.json: files[{index}]"
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            errors.append(f"{entry_label} must contain exactly path, bytes, and sha256")
            continue

        relative_posix = _manifest_path(entry.get("path"))
        if relative_posix is None or relative_posix.as_posix() == "index.json":
            errors.append(f"{entry_label} has an invalid relative JSON path")
            continue
        relative = Path("data", *relative_posix.parts)
        if _forbidden_path_category(relative) is not None:
            errors.append(f"{entry_label} declares a forbidden path")
            continue
        if relative in declared:
            errors.append(f"{entry_label} duplicates a manifest path")
            continue
        declared.add(relative)

        expected_bytes = entry.get("bytes")
        expected_sha256 = entry.get("sha256")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
        ):
            errors.append(f"{entry_label} has an invalid byte count")
            continue
        if not isinstance(expected_sha256, str) or not SHA256_PATTERN.fullmatch(
            expected_sha256
        ):
            errors.append(f"{entry_label} has an invalid SHA-256 digest")
            continue

        payload = payloads.get(relative)
        if payload is None or relative not in json_documents:
            errors.append(f"{entry_label} references a missing or invalid JSON file")
            continue
        if len(payload) != expected_bytes:
            errors.append(f"{entry_label} byte count does not match the artifact")
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if actual_sha256 != expected_sha256:
            errors.append(f"{entry_label} SHA-256 does not match the artifact")

    actual_json = set(json_documents) - {manifest_relative}
    json_outside_data = {
        path for path in actual_json if not path.parts or path.parts[0] != "data"
    }
    if json_outside_data:
        errors.append("JSON artifacts outside data/ are not allowed")
    if declared != actual_json:
        errors.append("data/index.json does not cover every and only public JSON artifact")
    if declared != FIXED_DATA_PATHS:
        errors.append("data/index.json: files do not match the fixed public data path set")
    return manifest


def validate_public_site(
    build_dir: Path | str,
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> list[str]:
    """Return safe validation errors for a generated Pages artifact."""
    root = Path(build_dir)
    errors: list[str] = []
    if max_file_bytes < 0 or max_total_bytes < 0:
        return ["size limits must be non-negative"]

    try:
        metadata = root.lstat()
    except OSError:
        return ["static-site build directory does not exist or is unreadable"]
    if stat.S_ISLNK(metadata.st_mode):
        return ["static-site build directory must not be a symbolic link"]
    if not stat.S_ISDIR(metadata.st_mode):
        return ["static-site build path is not a directory"]

    files = _walk_without_links(root, errors)
    if not files:
        errors.append("static-site build contains no regular files")
    _validate_artifact_layout(files, root=root, errors=errors)
    payloads, json_documents = _read_and_validate_files(
        files,
        root=root,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        errors=errors,
    )
    if not payloads.get(Path("index.html")):
        errors.append("index.html: missing or empty site entry point")
    _validate_index_security(payloads.get(Path("index.html")), errors)
    manifest = _validate_manifest(
        root=root,
        payloads=payloads,
        json_documents=json_documents,
        errors=errors,
    )
    _validate_fixed_payload_contracts(manifest, json_documents, errors)
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a generated static site before a Pages upload."
    )
    parser.add_argument(
        "build_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_BUILD_DIR,
        help="generated site directory (default: site/dist)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors = validate_public_site(args.build_dir)
    if errors:
        print("Public site validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Public site validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
