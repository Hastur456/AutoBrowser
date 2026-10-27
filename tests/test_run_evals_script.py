from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import run_evals as run_evals_script

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "tests" / "evals" / "baselines" / "agent_loop_v2.json"
V1_BASELINE_PATH = ROOT / "tests" / "evals" / "baselines" / "agent_loop_v1.json"


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


def _run_script(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_evals.py"), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )


def test_the_script_hides_a_personal_profile(tmp_path: Path) -> None:
    profile = tmp_path / "personal.yaml"
    profile.write_text("memory:\n  working_notes_max_chars: 1500\n", encoding="utf-8")
    env = {
        **os.environ,
        "AUTOBROWSER_CONFIG_FILE": str(profile),
        "AUTOBROWSER_LOOP__MAX_INEFFECTIVE_ACTIONS": "5",
        "PYTHONIOENCODING": "utf-8",
    }

    completed = _run_script(env)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_script_takes_the_tolerance_from_the_pipeline_environment(tmp_path: Path) -> None:
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    row = document["scenarios"]["search_basic"]
    row["request_chars"] = int(row["request_chars"] * 0.95)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    default = _run_script(env, "--baseline", str(path))
    strict = _run_script(
        {**env, "AUTOBROWSER_PIPELINE__REQUEST_CHARS_TOLERANCE": "0.01"}, "--baseline", str(path)
    )

    assert default.returncode == 0, default.stdout
    assert strict.returncode == 1
    assert "search_basic.request_chars: regression" in strict.stdout
