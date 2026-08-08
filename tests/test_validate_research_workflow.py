import copy
import json
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.validate_research_workflow import (  # noqa: E402
    validate_decision,
    validate_research_case,
    validate_review,
)


class ValidateResearchWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(
            (REPOSITORY_ROOT / "config/research_workflow_schema.json").read_text()
        )
        cls.research = json.loads(
            (REPOSITORY_ROOT / "research_queue/research_case.template.json").read_text()
        )
        cls.decision = json.loads(
            (REPOSITORY_ROOT / "decision_journal/decision_entry.template.json").read_text()
        )
        cls.review = json.loads(
            (REPOSITORY_ROOT / "decision_journal/review_entry.template.json").read_text()
        )

    def test_empty_templates_are_structurally_valid(self):
        self.assertEqual(validate_research_case(self.research, self.schema), [])
        self.assertEqual(validate_decision(self.decision, self.schema), [])
        self.assertEqual(validate_review(self.review, self.schema), [])

    def test_inference_must_reference_existing_facts(self):
        case = copy.deepcopy(self.research)
        case["facts"] = [
            {
                "fact_id": "F1",
                "claim": "fact",
                "source_url": "https://example.test",
                "as_of_date": "2026-08-08",
            }
        ]
        case["inferences"] = [
            {
                "claim": "inference",
                "supporting_fact_ids": ["F2"],
                "confidence": "high",
            }
        ]
        self.assertIn(
            "inferences[0] references unknown facts",
            validate_research_case(case, self.schema),
        )

    def test_decision_ready_requires_cases_red_team_and_breakers(self):
        case = copy.deepcopy(self.research)
        case["status"] = "decision_ready"
        errors = validate_research_case(case, self.schema)
        self.assertTrue(any("facts" in error for error in errors))
        self.assertTrue(any("valuation.bull" in error for error in errors))
        self.assertTrue(any("red-team" in error for error in errors))
        self.assertTrue(any("thesis_breakers" in error for error in errors))

    def test_final_decision_authority_cannot_be_ai(self):
        entry = copy.deepcopy(self.decision)
        entry["final_authority"] = "AI"
        self.assertIn(
            "final_authority must remain investor",
            validate_decision(entry, self.schema),
        )


if __name__ == "__main__":
    unittest.main()
