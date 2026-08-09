#!/usr/bin/env python3
"""Audit the complete investment-research workflow against explicit requirements."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.run_financial_collection_plan import inspect_plan, load_plan  # noqa: E402
from scripts.repository_paths import repository_relative_path  # noqa: E402
from scripts.validate_repository import validate_repository  # noqa: E402


DEFAULT_REQUIREMENTS = (
    REPOSITORY_ROOT / "config" / "system_completion_requirements.json"
)


def _json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _manifests(root: Path, area: str) -> List[Dict[str, Any]]:
    results = []
    for path in (root / "data" / area / "runs").glob("**/manifest.json"):
        manifest = _json(path)
        results.append({"path": path, "manifest": manifest})
    return results


def _result(requirement: str, achieved: bool, evidence: Any, gaps: List[str]) -> Dict[str, Any]:
    return {
        "requirement": requirement,
        "achieved": achieved,
        "evidence": evidence,
        "gaps": gaps,
    }


def audit_system(
    root: Path = REPOSITORY_ROOT,
    *,
    requirements_path: Path = DEFAULT_REQUIREMENTS,
) -> Dict[str, Any]:
    root = Path(root)
    requirements = _json(requirements_path)
    minimum = requirements["minimum_counts"]
    normalized = _manifests(root, "normalized")
    derived = _manifests(root, "derived")
    results = []

    integrity_errors = validate_repository(root)
    results.append(
        _result(
            "repository_integrity",
            not integrity_errors,
            {"validator": "scripts/validate_repository.py", "error_count": len(integrity_errors)},
            integrity_errors,
        )
    )

    raw_files = list((root / "data/raw/iwencai").glob("*/*/*/*.json"))
    query_logs = list((root / "data/raw/_query_log").glob("*/*/*.jsonl"))
    results.append(
        _result(
            "raw_data_infrastructure",
            bool(raw_files) and bool(query_logs),
            {"raw_snapshot_count": len(raw_files), "query_log_count": len(query_logs)},
            [] if raw_files and query_logs else ["raw snapshots or query logs are missing"],
        )
    )

    mapping = _json(root / "config/field_mappings.json")
    parser_ready = mapping.get("mapping_version") == "3.2.0" and (
        root / "scripts/parse_iwencai_fields.py"
    ).is_file()
    results.append(
        _result(
            "dynamic_field_parsing",
            parser_ready,
            {"mapping_version": mapping.get("mapping_version")},
            [] if parser_ready else ["mapping 3.2.0 parser evidence is missing"],
        )
    )

    market_candidates = []
    for item in normalized:
        coverage = item["manifest"].get("coverage", {})
        count = coverage.get("security_master_count", 0)
        if isinstance(count, int):
            market_candidates.append(
                (
                    count,
                    repository_relative_path(
                        item["path"], repository_root=root
                    ),
                )
            )
    best_market = max(market_candidates, default=(0, None))
    market_ready = best_market[0] >= minimum["full_market_securities"]
    results.append(
        _result(
            "full_market_database",
            market_ready,
            {"best_security_count": best_market[0], "manifest": best_market[1]},
            [] if market_ready else ["full-market normalized bundle is missing"],
        )
    )

    financial_periods: Dict[str, int] = {}
    financial_manifests = {}
    for item in normalized:
        coverage = item["manifest"].get("coverage", {})
        security_count = coverage.get("security_count", 0)
        for period in coverage.get("period_ends", []):
            if isinstance(security_count, int) and security_count > financial_periods.get(period, 0):
                financial_periods[period] = security_count
                financial_manifests[period] = repository_relative_path(
                    item["path"], repository_root=root
                )
    missing_financial = [
        period
        for period in requirements["required_financial_periods"]
        if financial_periods.get(period, 0) < minimum["full_financial_securities"]
    ]
    results.append(
        _result(
            "complete_financial_database",
            not missing_financial,
            {"security_count_by_period": financial_periods, "manifests": financial_manifests},
            [f"full financial period missing: {period}" for period in missing_financial],
        )
    )

    advanced_periods = set()
    advanced_calculated = 0
    for item in derived:
        table = item["manifest"].get("table", {})
        if table.get("logical_name") != "advanced_financial_metrics":
            continue
        advanced_calculated += item["manifest"].get("coverage", {}).get(
            "status_counts", {}
        ).get("calculated", 0)
        file_path = item["path"].parent / table["file"]
        if file_path.is_file():
            with file_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        row = json.loads(line)
                        if row.get("calculation_status") == "calculated":
                            advanced_periods.add(row.get("period_end"))
    missing_advanced = sorted(
        set(requirements["required_advanced_metric_periods"]) - advanced_periods
    )
    plan_status = inspect_plan(
        load_plan(root / "config/financial_collection_plan.json"),
        raw_root=root / "data/raw",
    )
    advanced_ready = not missing_advanced and advanced_calculated > 0
    results.append(
        _result(
            "advanced_financial_metrics",
            advanced_ready,
            {
                "calculated_record_count": advanced_calculated,
                "calculated_periods": sorted(x for x in advanced_periods if x),
                "collection_plan": plan_status,
            },
            [f"advanced calculated period missing: {period}" for period in missing_advanced],
        )
    )

    event_records = 0
    news_records = 0
    for item in normalized:
        table = item["manifest"].get("table", {})
        if table.get("logical_name") == "events":
            event_records += table.get("record_count", 0)
        elif table.get("logical_name") == "news_items":
            news_records += table.get("record_count", 0)
    taxonomy = _json(root / "config/event_taxonomy.json")
    taxonomy_types = {rule["event_type"] for rule in taxonomy["rules"]}
    missing_event_types = sorted(
        set(requirements["required_event_types"]) - taxonomy_types
    )
    news_artifacts = [
        root / "scripts/collect_iwencai_news.py",
        root / "scripts/normalize_iwencai_news.py",
        root / "scripts/generate_daily_news_report.py",
        root / "reports/daily/2026-08-08-financial-news.md",
    ]
    news_ready = news_records > 0 and all(path.is_file() for path in news_artifacts)
    event_ready = (
        event_records >= minimum["real_events"]
        and not missing_event_types
        and news_ready
    )
    event_gaps = [f"event taxonomy missing: {name}" for name in missing_event_types]
    if not news_ready:
        event_gaps.append("real normalized news monitoring evidence is missing")
    results.append(
        _result(
            "announcement_and_news_monitoring",
            event_ready,
            {
                "real_event_count": event_records,
                "real_news_count": news_records,
                "taxonomy_version": taxonomy.get("taxonomy_version"),
                "event_types": sorted(taxonomy_types),
                "news_collector": news_ready,
            },
            event_gaps,
        )
    )

    portfolio_paths = [
        root / "config/portfolio_schema.json",
        root / "portfolio/holdings.template.csv",
        root / "portfolio/transactions.template.csv",
        root / "portfolio/investment_card.template.json",
        root / "scripts/validate_portfolio.py",
        root / "scripts/classify_portfolio_review.py",
    ]
    portfolio_ready = all(path.is_file() for path in portfolio_paths)
    results.append(
        _result(
            "privacy_safe_portfolio_management",
            portfolio_ready,
            {
                "artifacts": [
                    repository_relative_path(path, repository_root=root)
                    for path in portfolio_paths
                ]
            },
            [] if portfolio_ready else ["portfolio artifact set is incomplete"],
        )
    )

    screening_count = 0
    for item in derived:
        table = item["manifest"].get("table", {})
        if table.get("logical_name") == "market_research_queue":
            screening_count = max(screening_count, table.get("record_count", 0))
    screening_ready = (
        screening_count >= minimum["screening_universe"]
        and (root / "scripts/classify_portfolio_review.py").is_file()
    )
    results.append(
        _result(
            "market_and_portfolio_screening",
            screening_ready,
            {"largest_screening_universe": screening_count},
            [] if screening_ready else ["market or portfolio screening evidence is incomplete"],
        )
    )

    research_paths = [
        root / "research_queue/research_case.template.json",
        root / "decision_journal/decision_entry.template.json",
        root / "decision_journal/review_entry.template.json",
        root / "scripts/validate_research_workflow.py",
    ]
    research_ready = all(path.is_file() for path in research_paths)
    results.append(
        _result(
            "structured_research_and_decision_journal",
            research_ready,
            {
                "artifacts": [
                    repository_relative_path(path, repository_root=root)
                    for path in research_paths
                ]
            },
            [] if research_ready else ["research workflow artifact set is incomplete"],
        )
    )

    ci_ready = (root / ".github/workflows/validate.yml").is_file()
    results.append(
        _result(
            "continuous_validation",
            ci_ready,
            {"workflow": ".github/workflows/validate.yml"},
            [] if ci_ready else ["continuous validation workflow is missing"],
        )
    )

    workflow_schema = _json(root / "config/research_workflow_schema.json")
    authority_ready = workflow_schema.get("final_authority") == "investor"
    results.append(
        _result(
            "investor_final_authority_no_auto_trading",
            authority_ready,
            {"final_authority": workflow_schema.get("final_authority")},
            [] if authority_ready else ["final authority is not fixed to investor"],
        )
    )

    order = requirements["requirements"]
    by_name = {item["requirement"]: item for item in results}
    ordered_results = [by_name[name] for name in order]
    return {
        "requirements_version": requirements["requirements_version"],
        "complete": all(item["achieved"] for item in ordered_results),
        "achieved_count": sum(item["achieved"] for item in ordered_results),
        "requirement_count": len(ordered_results),
        "results": ordered_results,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit end-to-end system completion.")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit_system(args.root, requirements_path=args.requirements)
        content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(content)
            print(args.output)
        else:
            sys.stdout.write(content)
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 1 if args.require_complete and not result["complete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
