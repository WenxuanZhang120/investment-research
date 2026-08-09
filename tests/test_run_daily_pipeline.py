import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.run_daily_pipeline import (  # noqa: E402
    PipelineError,
    load_pipeline_config,
    pipeline_plan,
    run_pipeline,
    validate_pipeline_config,
    write_run_report,
)


class RunDailyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        self.script = self.root / "scripts/ok.py"
        self.script.write_text("print('ok')\n", encoding="utf-8")

    def config(self, command=None):
        steps = []
        if command is not None:
            steps.append(
                {
                    "step_id": "test_step",
                    "command": command,
                    "timeout_seconds": 30,
                }
            )
        return {
            "pipeline_version": "test",
            "purpose": "test",
            "llm_calls_allowed": False,
            "external_collection_enabled": False,
            "stages": [
                {"stage": "status", "steps": steps},
                {"stage": "normalization", "steps": []},
                {"stage": "derivation", "steps": []},
                {"stage": "reporting", "steps": []},
                {"stage": "validation", "steps": []},
            ],
        }

    def test_repository_config_is_offline_and_has_all_stages(self):
        config = load_pipeline_config(
            REPOSITORY_ROOT / "config/daily_pipeline.json",
            repository_root=REPOSITORY_ROOT,
        )
        self.assertFalse(config["llm_calls_allowed"])
        self.assertFalse(config["external_collection_enabled"])
        planned_count = config["readiness"]["planned_step_count"]
        if planned_count:
            self.assertEqual(config["readiness"]["status"], "work_planned")
        else:
            self.assertIn(
                config["readiness"]["status"],
                {"waiting_for_complete_input", "up_to_date"},
            )
        self.assertEqual(
            [stage["stage"] for stage in config["stages"]],
            [
                "status",
                "normalization",
                "derivation",
                "reporting",
                "validation",
            ],
        )

    def test_dry_run_plan_has_no_machine_local_paths(self):
        config = validate_pipeline_config(
            self.config(["scripts/ok.py"]), repository_root=self.root
        )
        serialized = json.dumps(pipeline_plan(config))
        self.assertNotIn(str(self.root), serialized)
        self.assertIn("scripts/ok.py", serialized)

    def test_rejects_llm_and_disabled_external_collection(self):
        llm_config = self.config(["scripts/ok.py"])
        llm_config["llm_calls_allowed"] = True
        with self.assertRaisesRegex(PipelineError, "llm_calls_allowed"):
            validate_pipeline_config(llm_config, repository_root=self.root)

        collector = self.root / "scripts/collect_example.py"
        collector.write_text("print('collect')\n", encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "external collection is disabled"):
            validate_pipeline_config(
                self.config(["scripts/collect_example.py"]),
                repository_root=self.root,
            )

        collection_plan = self.root / "scripts/run_financial_collection_plan.py"
        collection_plan.write_text("print('collect')\n", encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "external collection is disabled"):
            validate_pipeline_config(
                self.config(
                    ["scripts/run_financial_collection_plan.py", "collect"]
                ),
                repository_root=self.root,
            )

        guarded = self.root / "scripts/run_guarded_financial_collection.py"
        guarded.write_text("print('collect')\n", encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "external collection is disabled"):
            validate_pipeline_config(
                self.config(
                    [
                        "scripts/run_guarded_financial_collection.py",
                        "--action",
                        "collect",
                    ]
                ),
                repository_root=self.root,
            )

    def test_rejects_absolute_command_arguments(self):
        with self.assertRaisesRegex(PipelineError, "absolute paths"):
            validate_pipeline_config(
                self.config(["scripts/ok.py", str(self.root / "input.json")]),
                repository_root=self.root,
            )

    def test_runs_without_shell_and_writes_portable_audit_report(self):
        config = validate_pipeline_config(
            self.config(["scripts/ok.py"]), repository_root=self.root
        )
        report = run_pipeline(
            config,
            repository_root=self.root,
            run_at="2026-08-09T10:00:00+08:00",
        )
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["step_count"], 1)
        self.assertEqual(report["steps"][0]["command"], ["scripts/ok.py"])
        self.assertNotIn("stdout", report["steps"][0])
        destination = write_run_report(
            report,
            reports_root=self.root / "reports/daily",
            repository_root=self.root,
        )
        content = destination.read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), content)
        self.assertIn('"llm_calls_allowed": false', content)

    def test_failed_step_stops_later_stages(self):
        failed = self.root / "scripts/fail.py"
        later = self.root / "scripts/later.py"
        failed.write_text("raise SystemExit(2)\n", encoding="utf-8")
        later.write_text("print('later')\n", encoding="utf-8")
        config = self.config(["scripts/fail.py"])
        config["stages"][-1]["steps"] = [
            {
                "step_id": "later_step",
                "command": ["scripts/later.py"],
                "timeout_seconds": 30,
            }
        ]
        validated = validate_pipeline_config(config, repository_root=self.root)
        report = run_pipeline(
            validated,
            repository_root=self.root,
            run_at="2026-08-09T10:00:00+08:00",
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["step_count"], 1)
        self.assertEqual(report["steps"][0]["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
