#!/usr/bin/env python3
"""Derive auditable financial ratios from normalized financial facts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.repository_paths import repository_relative_path  # noqa: E402

DEFAULT_METRIC_CONFIG = REPOSITORY_ROOT / "config" / "financial_metrics.json"
DEFAULT_DERIVED_ROOT = REPOSITORY_ROOT / "data" / "derived"
CALCULATOR_VERSION = "1.0.1"
DERIVED_BUNDLE_SCHEMA_VERSION = 1
REQUIRED_FACT_FIELDS = {
    "security_code",
    "security_name",
    "period_end",
    "report_type",
    "filing_date",
    "available_from",
    "fetched_at",
    "raw_record_id",
    "canonical_field_name",
    "value",
    "value_status",
}


class DerivationError(ValueError):
    """Raised when derived metrics cannot be calculated reproducibly."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise DerivationError(f"JSON root must be an object: {path}")
    return document


def load_metric_definitions(path: Path = DEFAULT_METRIC_CONFIG) -> Dict[str, Any]:
    document = _read_json(path)
    version = document.get("metric_definition_version")
    metrics = document.get("metrics")
    if not isinstance(version, str) or not version:
        raise DerivationError("metric_definition_version must be a string")
    if not isinstance(metrics, list) or not metrics:
        raise DerivationError("metrics must be a non-empty array")
    seen = set()
    for metric in metrics:
        if not isinstance(metric, dict):
            raise DerivationError("each metric definition must be an object")
        required = {
            "metric_name",
            "numerator",
            "denominator",
            "denominator_rule",
            "description",
        }
        if not required.issubset(metric):
            raise DerivationError("metric definition is incomplete")
        if metric["metric_name"] in seen:
            raise DerivationError(f"duplicate metric: {metric['metric_name']}")
        if metric["denominator_rule"] not in {"nonzero", "positive"}:
            raise DerivationError("unsupported denominator_rule")
        seen.add(metric["metric_name"])
    return document


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise DerivationError(f"{path}:{line_number} is not an object")
            yield value


def _fact_files(manifest: Dict[str, Any], bundle_dir: Path) -> List[Path]:
    table = manifest.get("tables", {}).get("financial_facts")
    if not isinstance(table, dict):
        raise DerivationError("source manifest has no financial_facts table")
    partitions = table.get("partitions")
    if isinstance(partitions, list):
        files = []
        for partition in partitions:
            if not isinstance(partition, dict):
                raise DerivationError("financial fact partition must be an object")
            path = bundle_dir / partition["file"]
            if _sha256(path) != partition.get("sha256"):
                raise DerivationError(f"financial fact hash mismatch: {path}")
            files.append(path)
        return files

    filename = table.get("file")
    if not isinstance(filename, str):
        raise DerivationError("financial_facts has no file or partitions")
    path = bundle_dir / filename
    if _sha256(path) != table.get("sha256"):
        raise DerivationError(f"financial fact hash mismatch: {path}")
    return [path]


def _load_latest_facts(
    manifest: Dict[str, Any],
    bundle_dir: Path,
) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    latest: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for path in _fact_files(manifest, bundle_dir):
        for fact in _iter_jsonl(path):
            if not REQUIRED_FACT_FIELDS.issubset(fact):
                raise DerivationError(f"financial fact is incomplete in {path}")
            key = (
                fact["security_code"],
                fact["period_end"],
                fact["canonical_field_name"],
            )
            existing = latest.get(key)
            ordering = (fact["fetched_at"], fact["raw_record_id"])
            if existing is None or ordering > (
                existing["fetched_at"],
                existing["raw_record_id"],
            ):
                latest[key] = fact
    return latest


def _calculate_metric(
    numerator: Optional[Any],
    denominator: Optional[Any],
    denominator_rule: str,
) -> Tuple[Optional[float], str]:
    if numerator is None or denominator is None:
        return None, "missing_inputs"
    if not isinstance(numerator, (int, float)) or isinstance(numerator, bool):
        raise DerivationError("metric numerator must be numeric")
    if not isinstance(denominator, (int, float)) or isinstance(denominator, bool):
        raise DerivationError("metric denominator must be numeric")
    if not math.isfinite(numerator) or not math.isfinite(denominator):
        raise DerivationError("metric inputs must be finite")
    if denominator == 0:
        return None, "zero_denominator"
    if denominator_rule == "positive" and denominator < 0:
        return None, "non_positive_denominator"
    return round(numerator / denominator, 12), "calculated"


def build_derived_metrics(
    source_manifest_path: Path,
    *,
    metric_config_path: Path = DEFAULT_METRIC_CONFIG,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    source_manifest_path = Path(source_manifest_path)
    source_bundle_dir = source_manifest_path.parent
    source_manifest = _read_json(source_manifest_path)
    metric_config = load_metric_definitions(metric_config_path)
    latest_facts = _load_latest_facts(source_manifest, source_bundle_dir)

    facts_by_report: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    for (security_code, period_end, field_name), fact in latest_facts.items():
        facts_by_report.setdefault((security_code, period_end), {})[field_name] = fact

    records = []
    for (security_code, period_end), period_facts in sorted(facts_by_report.items()):
        identity_fact = next(iter(period_facts.values()))
        for definition in metric_config["metrics"]:
            numerator_fact = period_facts.get(definition["numerator"])
            denominator_fact = period_facts.get(definition["denominator"])
            numerator = (
                numerator_fact["value"]
                if numerator_fact is not None
                and numerator_fact["value_status"] == "present"
                else None
            )
            denominator = (
                denominator_fact["value"]
                if denominator_fact is not None
                and denominator_fact["value_status"] == "present"
                else None
            )
            value, status = _calculate_metric(
                numerator,
                denominator,
                definition["denominator_rule"],
            )
            input_facts = []
            for role, field_name, fact in (
                ("numerator", definition["numerator"], numerator_fact),
                ("denominator", definition["denominator"], denominator_fact),
            ):
                input_facts.append(
                    {
                        "role": role,
                        "canonical_field_name": field_name,
                        "value": fact["value"] if fact else None,
                        "value_status": (
                            fact["value_status"] if fact else "fact_not_available"
                        ),
                        "raw_record_id": fact["raw_record_id"] if fact else None,
                        "raw_snapshot": fact.get("raw_snapshot") if fact else None,
                        "fetched_at": fact["fetched_at"] if fact else None,
                    }
                )
            input_records = [
                fact for fact in (numerator_fact, denominator_fact) if fact is not None
            ]
            available_from = max(
                (fact["available_from"] for fact in input_records),
                default=identity_fact["available_from"],
            )
            fetched_at = max(
                (fact["fetched_at"] for fact in input_records),
                default=identity_fact["fetched_at"],
            )
            records.append(
                {
                    "record_schema_version": 1,
                    "security_code": security_code,
                    "security_name": identity_fact["security_name"],
                    "period_end": period_end,
                    "report_type": identity_fact["report_type"],
                    "filing_date": identity_fact["filing_date"],
                    "available_from": available_from,
                    "fetched_at": fetched_at,
                    "metric_name": definition["metric_name"],
                    "value": value,
                    "unit": metric_config["value_unit"],
                    "calculation_status": status,
                    "formula": (
                        f"{definition['numerator']} / {definition['denominator']}"
                    ),
                    "annualized": False,
                    "metric_definition_version": metric_config[
                        "metric_definition_version"
                    ],
                    "calculator_version": CALCULATOR_VERSION,
                    "source_financial_bundle_id": source_manifest["bundle_id"],
                    "source_financial_manifest": repository_relative_path(
                        source_manifest_path, repository_root=repository_root
                    ),
                    "input_facts": input_facts,
                }
            )

    records.sort(
        key=lambda item: (
            item["security_code"],
            item["period_end"],
            item["metric_name"],
        )
    )
    primary_keys = [
        (record["security_code"], record["period_end"], record["metric_name"])
        for record in records
    ]
    if len(primary_keys) != len(set(primary_keys)):
        raise DerivationError("derived financial metrics contain duplicate keys")

    source_bundle_id = source_manifest["bundle_id"]
    identity = "\0".join(
        (
            CALCULATOR_VERSION,
            metric_config["metric_definition_version"],
            source_bundle_id,
        )
    ).encode("utf-8")
    return {
        "bundle_id": hashlib.sha256(identity).hexdigest()[:20],
        "source_manifest": source_manifest_path,
        "source_bundle_id": source_bundle_id,
        "source_fetched_at_start": source_manifest["fetched_at_start"],
        "source_fetched_at_end": source_manifest["fetched_at_end"],
        "metric_definition_version": metric_config["metric_definition_version"],
        "records": records,
        "coverage": {
            "security_count": len({record["security_code"] for record in records}),
            "period_ends": sorted({record["period_end"] for record in records}),
            "metric_count": len(metric_config["metrics"]),
            "record_count": len(records),
            "calculated_count": sum(
                record["calculation_status"] == "calculated" for record in records
            ),
            "unavailable_count": sum(
                record["calculation_status"] != "calculated" for record in records
            ),
        },
    }


def _jsonl_bytes(records: List[Dict[str, Any]]) -> bytes:
    return (
        "\n".join(
            json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for record in records
        )
        + "\n"
    ).encode("utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_derived_bundle(
    built: Dict[str, Any],
    *,
    derived_root: Path = DEFAULT_DERIVED_ROOT,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    fetched_at = datetime.fromisoformat(built["source_fetched_at_start"])
    destination = derived_root.joinpath(
        "runs",
        "iwencai",
        fetched_at.strftime("%Y"),
        fetched_at.strftime("%m"),
        fetched_at.strftime("%d"),
        built["bundle_id"],
    )
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite derived bundle: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".deriving-", dir=destination.parent))
    try:
        partitions = []
        period_groups: Dict[str, List[Dict[str, Any]]] = {}
        for record in built["records"]:
            period_groups.setdefault(record["period_end"], []).append(record)
        for period_end, records in sorted(period_groups.items()):
            filename = f"financial_metrics_{period_end}.jsonl"
            content = _jsonl_bytes(records)
            _write_bytes(staging / filename, content)
            partitions.append(
                {
                    "file": filename,
                    "period_end": period_end,
                    "record_count": len(records),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        manifest = {
            "bundle_schema_version": DERIVED_BUNDLE_SCHEMA_VERSION,
            "calculator_version": CALCULATOR_VERSION,
            "metric_definition_version": built["metric_definition_version"],
            "bundle_id": built["bundle_id"],
            "source_financial_bundle_id": built["source_bundle_id"],
            "source_financial_manifest": repository_relative_path(
                built["source_manifest"], repository_root=repository_root
            ),
            "source_fetched_at_start": built["source_fetched_at_start"],
            "source_fetched_at_end": built["source_fetched_at_end"],
            "coverage": built["coverage"],
            "table": {
                "logical_name": "financial_metrics",
                "primary_key": ["security_code", "period_end", "metric_name"],
                "partition_key": "period_end",
                "partitions": partitions,
            },
        }
        manifest_content = (
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2)
            + "\n"
        ).encode("utf-8")
        _write_bytes(staging / "manifest.json", manifest_content)
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return destination


def derive_financial_metrics(
    source_manifest_path: Path,
    *,
    metric_config_path: Path = DEFAULT_METRIC_CONFIG,
    derived_root: Path = DEFAULT_DERIVED_ROOT,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    built = build_derived_metrics(
        source_manifest_path,
        metric_config_path=metric_config_path,
        repository_root=repository_root,
    )
    return write_derived_bundle(
        built,
        derived_root=derived_root,
        repository_root=repository_root,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Derive versioned financial ratios from a financial manifest."
    )
    parser.add_argument("source_manifest", type=Path)
    parser.add_argument(
        "--metric-config",
        type=Path,
        default=DEFAULT_METRIC_CONFIG,
    )
    parser.add_argument("--derived-root", type=Path, default=DEFAULT_DERIVED_ROOT)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        destination = derive_financial_metrics(
            args.source_manifest,
            metric_config_path=args.metric_config,
            derived_root=args.derived_root,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
