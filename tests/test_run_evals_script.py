from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_evals as run_evals_script

BASELINE_PATH = Path("tests/evals/baselines/agent_loop_v2.json")
V1_BASELINE_PATH = Path("tests/evals/baselines/agent_loop_v1.json")


def _main(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr("sys.argv", ["run_evals.py", *args])
    return run_evals_script.main()


def test_main_exits_0_on_the_committed_baseline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _main(monkeypatch, "--baseline", str(BASELINE_PATH)) == 0
    assert "scenarios match the baseline" in capsys.readouterr().out


def test_main_reports_a_changed_metric_with_exit_code_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    actual = document["scenarios"]["search_basic"]["tool_call_count"]
    document["scenarios"]["search_basic"]["tool_call_count"] = actual + 1
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert _main(monkeypatch, "--baseline", str(path)) == 1
    out = capsys.readouterr().out
    assert f"search_basic.tool_call_count: changed {actual + 1} -> {actual}" in out


def test_a_v1_baseline_is_a_pipeline_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _main(monkeypatch, "--baseline", str(V1_BASELINE_PATH)) == 2
    assert "schema_version 2" in capsys.readouterr().err


def test_write_baseline_creates_a_baseline_the_next_run_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "baseline.json"

    assert _main(monkeypatch, "--baseline", str(path), "--write-baseline") == 0
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert "scenarios_with_memory" not in document
    assert _main(monkeypatch, "--baseline", str(path)) == 0
