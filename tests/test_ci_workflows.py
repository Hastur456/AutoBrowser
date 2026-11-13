"""The CI workflows keep the properties the scripts rely on (they run only on GitHub)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_the_suite_step_times_out_before_the_job_so_the_report_still_runs() -> None:
    # A job timeout is a cancellation: the `!cancelled()` report steps would be skipped and a
    # before-merge status would stay pending. A step timeout is a failure the report survives.
    job = _load("live-run.yml")["jobs"]["live"]
    suite = next(step for step in job["steps"] if step.get("name") == "Run the suite")

    assert "timeout-minutes" in suite
    assert suite["timeout-minutes"] + 10 <= job["timeout-minutes"]
