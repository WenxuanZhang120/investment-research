#!/usr/bin/env python3
"""Derive cross-period growth, ROE, ROIC, and free cash flow with explicit gaps."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.derive_financial_metrics import _load_latest_facts, _read_json  # noqa: E402


DEFAULT_CONFIG = REPOSITORY_ROOT / "config" / "advanced_financial_metrics.json"
DEFAULT_DERIVED_ROOT = REPOSITORY_ROOT / "data" / "derived"
CALCULATOR_VERSION = "1.0.0"
DEBT_FIELDS = (
    "short_term_borrowings",
    "non_current_liabilities_due_within_one_year",
    "long_term_borrowings",
    "bonds_payable",
)


def _prior_period(period_end: str) -> str:
    parsed = date.fromisoformat(period_end)
    return parsed.replace(year=parsed.year - 1).isoformat()


def _value(facts: Dict[str, Dict[str, Any]], field: str) -> Optional[float]:
    fact = facts.get(field)
    if fact is None or fact.get("value_status") != "present":
        return None
    value = fact.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{field} must be finite numeric data")
    return float(value)


def _growth(current: Optional[float], prior: Optional[float]) -> Tuple[Optional[float], str]:
    if current is None:
        return None, "missing_current_inputs"
    if prior is None:
        return None, "missing_prior_inputs"
    if prior == 0:
        return None, "zero_denominator"
    return round(current / prior - 1, 12), "calculated"


def _average_ratio(
    numerator: Optional[float],
    current_base: Optional[float],
    prior_base: Optional[float],
) -> Tuple[Optional[float], str]:
    if numerator is None or current_base is None:
        return None, "missing_current_inputs"
    if prior_base is None:
        return None, "missing_prior_inputs"
    denominator = (current_base + prior_base) / 2
    if denominator == 0:
        return None, "zero_denominator"
    return round(numerator / denominator, 12), "calculated"


def _invested_capital(facts: Dict[str, Dict[str, Any]]) -> Optional[float]:
    values = [_value(facts, "total_equity"), _value(facts, "monetary_funds")]
    values.extend(_value(facts, field) for field in DEBT_FIELDS)
    if any(value is None for value in values):
        return None
    equity, cash, *debts = values
    return equity + sum(debts) - cash


def _roic(
    current: Dict[str, Dict[str, Any]],
    prior: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[float], str]:
    operating_profit = _value(current, "operating_profit")
    tax = _value(current, "income_tax_expense")
    total_profit = _value(current, "total_profit")
    current_capital = _invested_capital(current)
    if any(value is None for value in (operating_profit, tax, total_profit, current_capital)):
        return None, "missing_current_inputs"
    prior_capital = _invested_capital(prior)
    if prior_capital is None:
        return None, "missing_prior_inputs"
    if total_profit == 0:
        return None, "zero_tax_rate_denominator"
    average_capital = (current_capital + prior_capital) / 2
    if average_capital == 0:
        return None, "zero_denominator"
    effective_tax_rate = tax / total_profit
    nopat = operating_profit * (1 - effective_tax_rate)
    return round(nopat / average_capital, 12), "calculated"


def calculate_records(
    latest_facts: Dict[Tuple[str, str, str], Dict[str, Any]],
    *,
    source_bundle_ids: Sequence[str],
    definition_version: str,
) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    for (code, period_end, field), fact in latest_facts.items():
        grouped.setdefault((code, period_end), {})[field] = fact
    records = []
    for (code, period_end), current in sorted(grouped.items()):
        prior_end = _prior_period(period_end)
        prior = grouped.get((code, prior_end), {})
        identity = next(iter(current.values()))
        calculations = {
            "revenue_growth_yoy": _growth(
                _value(current, "revenue"), _value(prior, "revenue")
            ),
            "net_income_growth_yoy": _growth(
                _value(current, "net_income_parent"),
                _value(prior, "net_income_parent"),
            ),
            "roe_parent_average": _average_ratio(
                _value(current, "net_income_parent"),
                _value(current, "equity_parent"),
                _value(prior, "equity_parent"),
            ),
            "roic_average": _roic(current, prior),
        }
        cfo = _value(current, "net_cash_flow_operating")
        capex = _value(current, "capital_expenditure_cash")
        calculations["free_cash_flow"] = (
            (round(cfo - capex, 6), "calculated")
            if cfo is not None and capex is not None
            else (None, "missing_current_inputs")
        )
        for name, (value, status) in calculations.items():
            records.append(
                {
                    "record_schema_version": 1,
                    "security_code": code,
                    "security_name": identity["security_name"],
                    "period_end": period_end,
                    "prior_comparable_period_end": prior_end,
                    "available_from": max(
                        [fact["available_from"] for fact in current.values()]
                        + [fact["available_from"] for fact in prior.values()]
                    ),
                    "metric_name": name,
                    "value": value,
                    "unit": "CNY" if name == "free_cash_flow" else "ratio",
                    "calculation_status": status,
                    "annualized": False,
                    "calculator_version": CALCULATOR_VERSION,
                    "metric_definition_version": definition_version,
                    "source_financial_bundle_ids": list(source_bundle_ids),
                }
            )
    return records


def build_advanced_metrics(
    manifest_paths: Sequence[Path],
    *,
    config_path: Path = DEFAULT_CONFIG,
) -> Dict[str, Any]:
    config = _read_json(config_path)
    latest: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    bundle_ids = []
    fetched_starts = []
    for manifest_path in manifest_paths:
        manifest = _read_json(manifest_path)
        bundle_ids.append(manifest["bundle_id"])
        fetched_starts.append(manifest["fetched_at_start"])
        for key, fact in _load_latest_facts(manifest, manifest_path.parent).items():
            existing = latest.get(key)
            if existing is None or (fact["fetched_at"], fact["raw_record_id"]) > (
                existing["fetched_at"], existing["raw_record_id"]
            ):
                latest[key] = fact
    records = calculate_records(
        latest,
        source_bundle_ids=bundle_ids,
        definition_version=config["metric_definition_version"],
    )
    identity = "\0".join(
        [CALCULATOR_VERSION, config["metric_definition_version"]] + sorted(bundle_ids)
    ).encode()
    return {
        "bundle_id": hashlib.sha256(identity).hexdigest()[:20],
        "records": records,
        "source_bundle_ids": bundle_ids,
        "source_manifest_paths": [str(path) for path in manifest_paths],
        "fetched_at_start": min(fetched_starts),
        "definition_version": config["metric_definition_version"],
    }


def write_bundle(built: Dict[str, Any], *, derived_root: Path = DEFAULT_DERIVED_ROOT) -> Path:
    fetched = datetime.fromisoformat(built["fetched_at_start"])
    destination = derived_root.joinpath(
        "runs", "iwencai", fetched.strftime("%Y"), fetched.strftime("%m"),
        fetched.strftime("%d"), built["bundle_id"],
    )
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite derived bundle: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".advanced-metrics-", dir=destination.parent))
    try:
        content = (
            "\n".join(json.dumps(x, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True) for x in built["records"])
            + ("\n" if built["records"] else "")
        ).encode()
        (staging / "advanced_financial_metrics.jsonl").write_bytes(content)
        statuses: Dict[str, int] = {}
        for record in built["records"]:
            statuses[record["calculation_status"]] = statuses.get(record["calculation_status"], 0) + 1
        manifest = {
            "bundle_schema_version": 1,
            "bundle_id": built["bundle_id"],
            "calculator_version": CALCULATOR_VERSION,
            "metric_definition_version": built["definition_version"],
            "source_financial_bundle_ids": built["source_bundle_ids"],
            "source_financial_manifests": built["source_manifest_paths"],
            "coverage": {"record_count": len(built["records"]), "status_counts": statuses},
            "table": {
                "logical_name": "advanced_financial_metrics",
                "file": "advanced_financial_metrics.jsonl",
                "primary_key": ["security_code", "period_end", "metric_name"],
                "record_count": len(built["records"]),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return destination


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Derive advanced cross-period metrics.")
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--derived-root", type=Path, default=DEFAULT_DERIVED_ROOT)
    args = parser.parse_args(argv)
    try:
        destination = write_bundle(
            build_advanced_metrics(args.manifests, config_path=args.config),
            derived_root=args.derived_root,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
