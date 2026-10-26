from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_evals as run_evals_script

BASELINE_PATH = Path("tests/evals/baselines/agent_loop_v1.json")


def test_baseline_diff_names_changed_metrics_and_unmatched_rows() -> None:
    results = {"a": {"tool_call_count": 3, "terminal_status": "completed"}, "new": {}}
    baseline = {"a": {"tool_call_count": 2, "terminal_status": "completed"}, "gone": {}}

    assert run_evals_script.baseline_diff(results, baseline) == [
        "a.tool_call_count: baseline 2, actual 3",
        "gone: baseline row without a scenario",
        "new: no baseline row",
    ]


def test_main_reports_a_baseline_mismatch_with_exit_code_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    baseline["search_basic"]["tool_call_count"] += 1
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["run_evals.py", "--baseline", str(path)])

    assert run_evals_script.main() == 1
    out = capsys.readouterr().out
    expected = baseline["search_basic"]["tool_call_count"]
    assert f"search_basic.tool_call_count: baseline {expected}, actual {expected - 1}" in out


def test_main_exits_0_on_the_committed_baseline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["run_evals.py", "--baseline", str(BASELINE_PATH)])

    assert run_evals_script.main() == 0
    assert "scenarios match the baseline" in capsys.readouterr().out
