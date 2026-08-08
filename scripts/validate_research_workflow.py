#!/usr/bin/env python3
"""Validate research cases and decision journal entries without judging investments."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = REPOSITORY_ROOT / "config" / "research_workflow_schema.json"


def _object(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be an object")
    return value


def _ratio(value: Any) -> bool:
    return (
        value is None
        or isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def validate_research_case(case: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    errors = []
    required = {
        "research_id", "security_code", "priority", "status", "as_of_date",
        "facts", "inferences", "judgments", "business_analysis", "growth_analysis",
        "investment_thesis", "valuation", "red_team", "portfolio_fit",
        "position_sizing", "thesis_breakers", "monitoring_indicators", "source_links",
    }
    for field in sorted(required - set(case)):
        errors.append(f"{field} is required")
    if errors:
        return errors
    if case["status"] not in schema["research_statuses"]:
        errors.append("status is not allowed")
    if case["priority"] not in {"P0", "P1", "P2", "Reject"}:
        errors.append("priority is not allowed")
    if case["as_of_date"]:
        try:
            date.fromisoformat(case["as_of_date"])
        except (TypeError, ValueError):
            errors.append("as_of_date must be YYYY-MM-DD")
    fact_ids = set()
    for index, fact in enumerate(case["facts"]):
        if not isinstance(fact, dict):
            errors.append(f"facts[{index}] must be an object")
            continue
        fact_id = fact.get("fact_id")
        if not isinstance(fact_id, str) or not fact_id:
            errors.append(f"facts[{index}].fact_id is required")
        elif fact_id in fact_ids:
            errors.append(f"facts[{index}].fact_id is duplicated")
        fact_ids.add(fact_id)
        for field in ("claim", "source_url", "as_of_date"):
            if not fact.get(field):
                errors.append(f"facts[{index}].{field} is required")
    for index, inference in enumerate(case["inferences"]):
        if not isinstance(inference, dict):
            errors.append(f"inferences[{index}] must be an object")
            continue
        if inference.get("confidence") not in schema["confidence_values"]:
            errors.append(f"inferences[{index}].confidence is not allowed")
        references = inference.get("supporting_fact_ids")
        if not isinstance(references, list) or not references:
            errors.append(f"inferences[{index}].supporting_fact_ids is required")
        elif not set(references).issubset(fact_ids):
            errors.append(f"inferences[{index}] references unknown facts")
    if set(case["valuation"]) != set(schema["valuation_cases"]):
        errors.append("valuation must contain bull, base, and bear")
    if not _ratio(case["position_sizing"].get("proposed_weight")):
        errors.append("position_sizing.proposed_weight must be null or 0..1")
    ready_index = schema["research_statuses"].index("decision_ready")
    if schema["research_statuses"].index(case["status"]) >= ready_index:
        if not case["facts"]:
            errors.append("decision-ready research must contain facts")
        if not case["thesis_breakers"]:
            errors.append("decision-ready research must contain thesis_breakers")
        for valuation_case in schema["valuation_cases"]:
            value = case["valuation"][valuation_case]
            if not value.get("method") or value.get("estimated_value") is None:
                errors.append(f"decision-ready valuation.{valuation_case} is incomplete")
        if not any(case["red_team"].values()):
            errors.append("decision-ready research must contain red-team challenges")
    return errors


def validate_decision(entry: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    errors = []
    required = {
        "decision_id", "research_id", "security_code", "decision_at", "action",
        "final_authority", "facts_used", "inferences_used", "judgments_made",
        "base_case", "bear_case", "bull_case", "red_team_response", "portfolio_fit",
        "target_weight", "execution_plan", "thesis_breakers", "monitoring_plan",
        "reason_for_decision", "known_uncertainties", "private_execution_reference",
    }
    for field in sorted(required - set(entry)):
        errors.append(f"{field} is required")
    if errors:
        return errors
    if entry["action"] not in schema["decision_actions"]:
        errors.append("action is not allowed")
    if entry["final_authority"] != schema["final_authority"]:
        errors.append("final_authority must remain investor")
    if not _ratio(entry["target_weight"]):
        errors.append("target_weight must be null or 0..1")
    if entry["decision_at"]:
        try:
            datetime.fromisoformat(entry["decision_at"])
        except (TypeError, ValueError):
            errors.append("decision_at must be ISO 8601")
    if entry["action"] not in {"DEFER", "REJECT"}:
        for field in ("reason_for_decision", "thesis_breakers", "monitoring_plan"):
            if not entry[field]:
                errors.append(f"{field} is required for an active decision")
    return errors


def validate_review(entry: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    errors = []
    required = {
        "review_id", "decision_id", "reviewed_at", "review_horizon",
        "outcome_summary", "thesis_status", "what_was_right", "what_was_wrong",
        "process_errors", "luck_vs_skill", "new_evidence", "action_after_review",
        "rule_changes_proposed",
    }
    for field in sorted(required - set(entry)):
        errors.append(f"{field} is required")
    if errors:
        return errors
    if entry["thesis_status"] not in {"intact", "uncertain", "broken"}:
        errors.append("thesis_status is not allowed")
    if entry["action_after_review"] not in schema["decision_actions"]:
        errors.append("action_after_review is not allowed")
    return errors


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate research workflow artifacts.")
    parser.add_argument("kind", choices=("research", "decision", "review"))
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args(argv)
    try:
        schema = _object(args.schema)
        validator = {
            "research": validate_research_case,
            "decision": validate_decision,
            "review": validate_review,
        }[args.kind]
        errors = []
        for path in args.paths:
            for error in validator(_object(path), schema):
                errors.append(f"{path}: {error}")
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"validated {len(args.paths)} {args.kind} artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
