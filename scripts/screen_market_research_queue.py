#!/usr/bin/env python3
"""Rank securities for research using transparent point-in-time inputs."""

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
from typing import Any, Dict, Iterable, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.repository_paths import repository_relative_path  # noqa: E402
from scripts.investment_universe import (  # noqa: E402
    DEFAULT_UNIVERSE,
    load_investment_universe,
    stock_record_allowed,
)

DEFAULT_RULES = REPOSITORY_ROOT / "config" / "screening_rules.json"
DEFAULT_DERIVED_ROOT = REPOSITORY_ROOT / "data" / "derived"
SCREENER_VERSION = "1.1.0"


class ScreeningError(ValueError):
    pass


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ScreeningError(f"JSON root must be an object: {path}")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ScreeningError(f"row must be an object: {path}")
                yield value


def _load_valuations(manifest_path: Path) -> Dict[str, Dict[str, Any]]:
    manifest = _read_json(manifest_path)
    table = manifest.get("tables", {}).get("valuation_snapshots")
    if not isinstance(table, dict):
        raise ScreeningError("market manifest has no valuation_snapshots table")
    path = manifest_path.parent / table["file"]
    if _sha(path) != table.get("sha256"):
        raise ScreeningError("valuation snapshot hash mismatch")
    latest: Dict[str, Dict[str, Any]] = {}
    for row in _iter_jsonl(path):
        code = row["security_code"]
        existing = latest.get(code)
        if existing is None or (row["as_of_date"], row["fetched_at"]) > (
            existing["as_of_date"], existing["fetched_at"]
        ):
            latest[code] = row
    return latest


def _load_security_master(manifest_path: Path) -> Dict[str, Dict[str, Any]]:
    manifest = _read_json(manifest_path)
    table = manifest.get("tables", {}).get("security_master")
    if not isinstance(table, dict):
        raise ScreeningError("market manifest has no security_master table")
    path = manifest_path.parent / table["file"]
    if _sha(path) != table.get("sha256"):
        raise ScreeningError("security master hash mismatch")
    latest: Dict[str, Dict[str, Any]] = {}
    for row in _iter_jsonl(path):
        code = row["security_code"]
        existing = latest.get(code)
        if existing is None or (row["observed_date"], row["fetched_at"]) > (
            existing["observed_date"], existing["fetched_at"]
        ):
            latest[code] = row
    return latest


def _load_metrics(manifest_path: Path) -> Dict[str, Dict[str, Dict[str, Any]]]:
    manifest = _read_json(manifest_path)
    table = manifest.get("table")
    if not isinstance(table, dict) or table.get("logical_name") != "financial_metrics":
        raise ScreeningError("derived manifest is not financial_metrics")
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for partition in table.get("partitions", []):
        path = manifest_path.parent / partition["file"]
        if _sha(path) != partition.get("sha256"):
            raise ScreeningError("financial metric hash mismatch")
        for row in _iter_jsonl(path):
            code = row["security_code"]
            name = row["metric_name"]
            existing = result.setdefault(code, {}).get(name)
            if existing is None or (row["period_end"], row["fetched_at"]) > (
                existing["period_end"], existing["fetched_at"]
            ):
                result[code][name] = row
    return result


def _percentile_scores(values: Dict[str, float], direction: str) -> Dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    size = len(ordered)
    if size == 1:
        return {ordered[0][0]: 1.0}
    scores = {code: index / (size - 1) for index, (code, _) in enumerate(ordered)}
    if direction == "lower":
        scores = {code: 1 - score for code, score in scores.items()}
    return scores


def build_screen(
    market_manifest_path: Path,
    metric_manifest_path: Path,
    *,
    rules_path: Path = DEFAULT_RULES,
    universe_path: Path = DEFAULT_UNIVERSE,
) -> Dict[str, Any]:
    rules = _read_json(rules_path)
    universe = load_investment_universe(universe_path)
    security_master = _load_security_master(Path(market_manifest_path))
    allowed_codes = {
        code
        for code, record in security_master.items()
        if stock_record_allowed(record, universe)
    }
    valuations = _load_valuations(Path(market_manifest_path))
    metrics = _load_metrics(Path(metric_manifest_path))
    candidates = []
    required = rules["eligibility"]["required_metrics"]
    all_codes = sorted((set(valuations) | set(metrics)) & allowed_codes)
    for code in all_codes:
        valuation = valuations.get(code)
        code_metrics = metrics.get(code, {})
        reasons = []
        if valuation is None:
            reasons.append("missing_valuation")
        pe = valuation.get("pe_ttm") if valuation else None
        market_cap = valuation.get("market_cap") if valuation else None
        if not isinstance(pe, (int, float)) or not math.isfinite(pe):
            reasons.append("missing_pe_ttm")
        elif rules["eligibility"]["require_positive_pe_ttm"] and pe <= 0:
            reasons.append("non_positive_pe_ttm")
        if not isinstance(market_cap, (int, float)) or not math.isfinite(market_cap):
            reasons.append("missing_market_cap")
        elif rules["eligibility"]["require_positive_market_cap"] and market_cap <= 0:
            reasons.append("non_positive_market_cap")
        as_of = valuation["as_of_date"] if valuation else None
        values = {}
        security_name = None
        period_end = None
        available_from = None
        for name in required:
            metric = code_metrics.get(name)
            if metric is None or metric.get("calculation_status") != "calculated":
                reasons.append(f"missing_metric:{name}")
                continue
            if as_of and metric["available_from"] > as_of:
                reasons.append(f"not_available_at_screen_date:{name}")
                continue
            values[name] = metric["value"]
            security_name = security_name or metric.get("security_name")
            period_end = max(period_end or metric["period_end"], metric["period_end"])
            available_from = max(
                available_from or metric["available_from"], metric["available_from"]
            )
        candidates.append(
            {
                "security_code": code,
                "security_name": security_name,
                "as_of_date": as_of,
                "financial_period_end": period_end,
                "financial_available_from": available_from,
                "pe_ttm": pe,
                "market_cap": market_cap,
                **values,
                "eligibility_reasons": sorted(set(reasons)),
                "eligible": not reasons,
            }
        )

    eligible = [item for item in candidates if item["eligible"]]
    component_scores: Dict[str, Dict[str, float]] = {}
    for component in rules["components"]:
        field = component["field"]
        values = {item["security_code"]: item[field] for item in eligible}
        component_scores[field] = _percentile_scores(values, component["direction"])
    for item in candidates:
        if not item["eligible"]:
            item.update({"score": None, "score_components": {}, "priority": "Reject"})
            continue
        parts = {
            component["field"]: round(
                component_scores[component["field"]][item["security_code"]], 12
            )
            for component in rules["components"]
        }
        score = sum(
            parts[component["field"]] * component["weight"]
            for component in rules["components"]
        )
        item.update({"score": round(score, 12), "score_components": parts})
    ranked = sorted(
        [item for item in candidates if item["eligible"]],
        key=lambda item: (-item["score"], item["security_code"]),
    )
    size = len(ranked)
    for rank, item in enumerate(ranked, start=1):
        top_fraction = (size - rank + 1) / size
        if top_fraction >= rules["priority_percentiles"]["P0"]:
            priority = "P0"
        elif top_fraction >= rules["priority_percentiles"]["P1"]:
            priority = "P1"
        else:
            priority = "P2"
        item.update({"rank": rank, "priority": priority})
    for item in candidates:
        if not item["eligible"]:
            item["rank"] = None
    candidates.sort(
        key=lambda item: (
            item["rank"] is None,
            item["rank"] if item["rank"] is not None else 10**9,
            item["security_code"],
        )
    )
    identity = "\0".join(
        (
            SCREENER_VERSION,
            rules["screening_version"],
            universe["universe_version"],
            _sha(Path(market_manifest_path)),
            _sha(Path(metric_manifest_path)),
        )
    ).encode()
    return {
        "bundle_id": hashlib.sha256(identity).hexdigest()[:20],
        "records": candidates,
        "rules": rules,
        "universe": universe,
        "universe_path": Path(universe_path),
        "market_manifest": Path(market_manifest_path),
        "metric_manifest": Path(metric_manifest_path),
        "coverage": {
            "universe_count": len(candidates),
            "configured_stock_universe_id": universe["stocks"]["universe_id"],
            "excluded_by_universe_count": len(security_master) - len(allowed_codes),
            "eligible_count": len(eligible),
            "reject_count": len(candidates) - len(eligible),
            "priority_counts": {
                priority: sum(x["priority"] == priority for x in candidates)
                for priority in ("P0", "P1", "P2", "Reject")
            },
        },
    }


def write_screen(
    built: Dict[str, Any],
    *,
    derived_root: Path = DEFAULT_DERIVED_ROOT,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    observed_dates = [x["as_of_date"] for x in built["records"] if x["as_of_date"]]
    if not observed_dates:
        raise ScreeningError("screen has no as_of_date")
    as_of = date.fromisoformat(max(observed_dates))
    destination = derived_root.joinpath(
        "runs", "screening", as_of.strftime("%Y"), as_of.strftime("%m"),
        as_of.strftime("%d"), built["bundle_id"],
    )
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite screening bundle: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".screening-", dir=destination.parent))
    try:
        content = (
            "\n".join(json.dumps(x, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True) for x in built["records"])
            + "\n"
        ).encode()
        (staging / "market_research_queue.jsonl").write_bytes(content)
        manifest = {
            "bundle_schema_version": 1,
            "bundle_id": built["bundle_id"],
            "screener_version": SCREENER_VERSION,
            "screening_version": built["rules"]["screening_version"],
            "universe_version": built["universe"]["universe_version"],
            "universe_id": built["universe"]["stocks"]["universe_id"],
            "investment_universe": repository_relative_path(
                built["universe_path"], repository_root=repository_root
            ),
            "purpose": built["rules"]["purpose"],
            "cross_industry_preliminary": built["rules"]["cross_industry_preliminary"],
            "source_market_manifest": repository_relative_path(
                built["market_manifest"], repository_root=repository_root
            ),
            "source_metric_manifest": repository_relative_path(
                built["metric_manifest"], repository_root=repository_root
            ),
            "coverage": built["coverage"],
            "table": {
                "logical_name": "market_research_queue",
                "file": "market_research_queue.jsonl",
                "primary_key": ["security_code", "as_of_date", "screening_version"],
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
    parser = argparse.ArgumentParser(description="Build a preliminary research queue.")
    parser.add_argument("market_manifest", type=Path)
    parser.add_argument("metric_manifest", type=Path)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--derived-root", type=Path, default=DEFAULT_DERIVED_ROOT)
    args = parser.parse_args(argv)
    try:
        destination = write_screen(
            build_screen(
                args.market_manifest,
                args.metric_manifest,
                rules_path=args.rules,
                universe_path=args.universe,
            ),
            derived_root=args.derived_root,
            repository_root=REPOSITORY_ROOT,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
