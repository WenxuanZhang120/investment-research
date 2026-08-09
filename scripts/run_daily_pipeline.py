#!/usr/bin/env python3
"""Run the versioned daily pipeline without shell or LLM dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.repository_paths import repository_relative_path  # noqa: E402
from scripts.resolve_daily_pipeline import resolve_pipeline_config  # noqa: E402


DEFAULT_CONFIG = REPOSITORY_ROOT / "config" / "daily_pipeline.json"
DEFAULT_REPORTS_ROOT = REPOSITORY_ROOT / "reports" / "daily"
REQUIRED_STAGES = (
    "status",
    "normalization",
    "derivation",
    "reporting",
    "validation",
)
WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class PipelineError(ValueError):
    """Raised when a pipeline cannot be planned or run safely."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_command(
    command: Any,
    *,
    repository_root: Path,
    external_collection_enabled: bool,
) -> List[str]:
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(arg, str) or not arg for arg in command)
    ):
        raise PipelineError("each pipeline command must be a non-empty string array")
    for argument in command:
        if Path(argument).is_absolute() or WINDOWS_ABSOLUTE_PATH.match(argument):
            raise PipelineError("pipeline commands must not contain absolute paths")
    script = Path(command[0])
    if script.parts[:1] != ("scripts",) or script.suffix != ".py":
        raise PipelineError("pipeline commands must invoke a repository scripts/*.py file")
    resolved_script = (repository_root / script).resolve()
    try:
        resolved_script.relative_to(repository_root.resolve())
    except ValueError as error:
        raise PipelineError("pipeline command escapes repository root") from error
    if not resolved_script.is_file():
        raise PipelineError(f"pipeline script does not exist: {script.as_posix()}")
    if script.name == Path(__file__).name:
        raise PipelineError("daily pipeline must not invoke itself")
    collection_requested = script.name.startswith("collect_") or (
        script.name == "run_financial_collection_plan.py"
        and "collect" in command[1:]
    ) or (
        script.name == "run_guarded_financial_collection.py"
        and "collect" in command[1:]
    )
    if not external_collection_enabled and collection_requested:
        raise PipelineError(
            "external collection is disabled but a collector step is configured"
        )
    return list(command)


def validate_pipeline_config(
    config: Dict[str, Any],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    if not isinstance(config, dict):
        raise PipelineError("pipeline config root must be an object")
    version = config.get("pipeline_version")
    if not isinstance(version, str) or not version:
        raise PipelineError("pipeline_version must be a non-empty string")
    if config.get("llm_calls_allowed") is not False:
        raise PipelineError("daily pipeline must set llm_calls_allowed to false")
    external_enabled = config.get("external_collection_enabled")
    if not isinstance(external_enabled, bool):
        raise PipelineError("external_collection_enabled must be boolean")
    stages = config.get("stages")
    if not isinstance(stages, list):
        raise PipelineError("stages must be an array")
    stage_names = [stage.get("stage") for stage in stages if isinstance(stage, dict)]
    if tuple(stage_names) != REQUIRED_STAGES or len(stage_names) != len(stages):
        raise PipelineError(
            "pipeline stages must appear once in required execution order: "
            + ", ".join(REQUIRED_STAGES)
        )

    seen_step_ids = set()
    normalized_stages = []
    for stage in stages:
        steps = stage.get("steps")
        if not isinstance(steps, list):
            raise PipelineError(f"stage {stage['stage']} steps must be an array")
        normalized_steps = []
        for step in steps:
            if not isinstance(step, dict):
                raise PipelineError("each pipeline step must be an object")
            step_id = step.get("step_id")
            if not isinstance(step_id, str) or not step_id:
                raise PipelineError("every pipeline step must have a step_id")
            if step_id in seen_step_ids:
                raise PipelineError(f"duplicate pipeline step_id: {step_id}")
            seen_step_ids.add(step_id)
            timeout = step.get("timeout_seconds", 300)
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                raise PipelineError(f"invalid timeout_seconds for step {step_id}")
            command = _validate_command(
                step.get("command"),
                repository_root=repository_root,
                external_collection_enabled=external_enabled,
            )
            normalized_steps.append(
                {
                    "step_id": step_id,
                    "command": command,
                    "timeout_seconds": timeout,
                }
            )
        normalized_stages.append(
            {"stage": stage["stage"], "steps": normalized_steps}
        )
    return {
        "pipeline_version": version,
        "purpose": config.get("purpose"),
        "llm_calls_allowed": False,
        "external_collection_enabled": external_enabled,
        "readiness": config.get("readiness"),
        "stages": normalized_stages,
    }


def load_pipeline_config(
    path: Path = DEFAULT_CONFIG,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> Dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    document = resolve_pipeline_config(document, repository_root=repository_root)
    return validate_pipeline_config(document, repository_root=repository_root)


def pipeline_plan(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pipeline_version": config["pipeline_version"],
        "purpose": config.get("purpose"),
        "llm_calls_allowed": False,
        "external_collection_enabled": config["external_collection_enabled"],
        "readiness": config.get("readiness"),
        "stages": config["stages"],
    }


def _timestamp(value: Optional[str]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PipelineError("run-at must include a timezone")
    return parsed


def run_pipeline(
    config: Dict[str, Any],
    *,
    repository_root: Path = REPOSITORY_ROOT,
    run_at: Optional[str] = None,
) -> Dict[str, Any]:
    started = _timestamp(run_at)
    started_text = started.isoformat(timespec="microseconds")
    config_sha256 = _sha256_bytes(_canonical(config))
    run_id = _sha256_bytes(
        f"{config['pipeline_version']}\0{config_sha256}\0{started_text}".encode("utf-8")
    )[:20]
    results = []
    overall_status = "succeeded"
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    for stage in config["stages"]:
        for step in stage["steps"]:
            command = [sys.executable, *step["command"]]
            step_started = datetime.now(timezone.utc)
            try:
                completed = subprocess.run(
                    command,
                    cwd=repository_root,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=step["timeout_seconds"],
                    check=False,
                )
                stdout = completed.stdout
                stderr = completed.stderr
                exit_code = completed.returncode
                status = "succeeded" if exit_code == 0 else "failed"
                error_type = None
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout or b""
                stderr = error.stderr or b""
                exit_code = None
                status = "failed"
                error_type = "timeout"
            step_finished = datetime.now(timezone.utc)
            results.append(
                {
                    "stage": stage["stage"],
                    "step_id": step["step_id"],
                    "command": step["command"],
                    "status": status,
                    "exit_code": exit_code,
                    "error_type": error_type,
                    "started_at": step_started.isoformat(timespec="microseconds"),
                    "finished_at": step_finished.isoformat(timespec="microseconds"),
                    "stdout_sha256": _sha256_bytes(stdout),
                    "stdout_bytes": len(stdout),
                    "stderr_sha256": _sha256_bytes(stderr),
                    "stderr_bytes": len(stderr),
                }
            )
            if stdout:
                sys.stdout.write(stdout.decode("utf-8", errors="replace"))
            if stderr:
                sys.stderr.write(stderr.decode("utf-8", errors="replace"))
            if status == "failed":
                overall_status = "failed"
                break
        if overall_status == "failed":
            break

    finished = datetime.now(timezone.utc)
    return {
        "run_schema_version": 1,
        "run_id": run_id,
        "pipeline_version": config["pipeline_version"],
        "config_sha256": config_sha256,
        "llm_calls_allowed": False,
        "external_collection_enabled": config["external_collection_enabled"],
        "readiness": config.get("readiness"),
        "status": overall_status,
        "started_at": started_text,
        "finished_at": finished.isoformat(timespec="microseconds"),
        "step_count": len(results),
        "steps": results,
    }


def write_run_report(
    report: Dict[str, Any],
    *,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    reports_root = Path(reports_root)
    repository_relative_path(reports_root, repository_root=repository_root)
    run_date = datetime.fromisoformat(report["started_at"]).date().isoformat()
    destination = reports_root / "pipeline-runs" / run_date / (
        report["run_id"] + ".json"
    )
    repository_relative_path(destination, repository_root=repository_root)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite pipeline report: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        report,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".pipeline-run-", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.rename(destination)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return destination


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic daily research-data pipeline."
    )
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--reports-root", type=Path)
    parser.add_argument("--run-at")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    config_path = args.config or root / "config" / "daily_pipeline.json"
    reports_root = args.reports_root or root / "reports" / "daily"
    try:
        config = load_pipeline_config(config_path, repository_root=root)
        if args.dry_run:
            print(json.dumps(pipeline_plan(config), ensure_ascii=False, indent=2))
            return 0
        report = run_pipeline(config, repository_root=root, run_at=args.run_at)
        if not args.no_report:
            destination = write_run_report(
                report,
                reports_root=reports_root,
                repository_root=root,
            )
            print(repository_relative_path(destination, repository_root=root))
        return 0 if report["status"] == "succeeded" else 1
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
