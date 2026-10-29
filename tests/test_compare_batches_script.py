"""scripts/compare_batches.py: loads two batch runs, prints the verdict, writes compare.md."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import compare_batches as script


def _batch(
    root: Path,
    batch_id: str,
    started_at: str,
    rows: list[tuple[str, str]],
    *,
    tasks: str = "t",
    duration_ms: int = 10_000,
) -> Path:
    batch_dir = root / batch_id
    batch_dir.mkdir(parents=True)
    meta = {
        "schema_version": 1,
        "batch_id": batch_id,
        "started_at": started_at,
        "provenance": {"tasks_sha256": tasks, "settings_sha256": "s", "model": "m"},
    }
    (batch_dir / "batch.json").write_text(json.dumps(meta), encoding="utf-8")
    lines = [
        json.dumps(
            {"scenario_id": scenario, "failure_kind": kind, "metrics": {"duration_ms": duration_ms}}
        )
        for scenario, kind in rows
    ]
    (batch_dir / "run_index.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return batch_dir


def _run(root: Path, *argv: str) -> int:
    return script.main(["--batches-dir", str(root), *argv])


def test_two_named_batches_are_compared_and_the_newer_gets_compare_md(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _batch(tmp_path, "batch-a", "2026-10-29T10:00:00+00:00", [("s1", "passed")])
    newer = _batch(tmp_path, "batch-b", "2026-10-29T11:00:00+00:00", [("s1", "guard_limit")])

    code = _run(tmp_path, "batch-a", "batch-b")

    out = capsys.readouterr().out
    assert code == 1
    assert "regression: s1 passed -> guard_limit" in out
    assert "| regression | s1 | passed | guard_limit |" in (newer / "compare.md").read_text(encoding="utf-8")


def test_batches_can_be_named_by_directory(tmp_path: Path) -> None:
    older = _batch(tmp_path / "x", "batch-a", "2026-10-29T10:00:00+00:00", [("s1", "passed")])
    newer = _batch(tmp_path / "y", "batch-b", "2026-10-29T11:00:00+00:00", [("s1", "passed")])

    assert _run(tmp_path, str(older), str(newer)) == 0


def test_without_arguments_the_latest_run_meets_the_previous_comparable_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _batch(tmp_path, "batch-old", "2026-10-29T09:00:00+00:00", [("s1", "guard_limit")])
    _batch(tmp_path, "batch-prev", "2026-10-29T10:00:00+00:00", [("s1", "passed")])
    _batch(tmp_path, "batch-other-set", "2026-10-29T11:00:00+00:00", [("s1", "passed")], tasks="x")
    latest = _batch(tmp_path, "batch-latest", "2026-10-29T12:00:00+00:00", [("s1", "provider_error")])

    code = _run(tmp_path)

    out = capsys.readouterr().out
    assert code == 0
    assert "batch-prev -> batch-latest" in out
    assert "inconclusive: s1 passed -> provider_error" in out
    assert (latest / "compare.md").is_file()


def test_json_output_is_machine_readable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _batch(tmp_path, "batch-a", "2026-10-29T10:00:00+00:00", [("s1", "guard_limit")])
    _batch(tmp_path, "batch-b", "2026-10-29T11:00:00+00:00", [("s1", "passed")])

    code = _run(tmp_path, "batch-a", "batch-b", "--json")

    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["findings"][0]["kind"] == "fixed"
    assert payload["exit_code"] == 0


def test_perf_findings_fail_only_with_the_perf_gate(tmp_path: Path) -> None:
    _batch(tmp_path, "batch-a", "2026-10-29T10:00:00+00:00", [("s1", "passed")], duration_ms=1_000)
    _batch(tmp_path, "batch-b", "2026-10-29T11:00:00+00:00", [("s1", "passed")], duration_ms=100_000)

    assert _run(tmp_path, "batch-a", "batch-b") == 0
    assert _run(tmp_path, "batch-a", "batch-b", "--perf-gate") == 3


def test_different_golden_sets_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _batch(tmp_path, "batch-a", "2026-10-29T10:00:00+00:00", [("s1", "passed")])
    _batch(tmp_path, "batch-b", "2026-10-29T11:00:00+00:00", [("s1", "passed")], tasks="other")

    assert _run(tmp_path, "batch-a", "batch-b") == 2
    assert "not comparable" in capsys.readouterr().err


def test_an_unreadable_batch_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _batch(tmp_path, "batch-a", "2026-10-29T10:00:00+00:00", [("s1", "passed")])

    assert _run(tmp_path, "batch-a", "batch-missing") == 2
    assert "error:" in capsys.readouterr().err


def test_without_a_previous_comparable_run_it_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _batch(tmp_path, "batch-only", "2026-10-29T10:00:00+00:00", [("s1", "passed")])

    assert _run(tmp_path) == 2
    assert "no previous comparable run" in capsys.readouterr().err


def test_an_older_batch_without_provenance_gets_its_hash_from_its_tasks_copy(tmp_path: Path) -> None:
    older = tmp_path / "batch-a"
    older.mkdir()
    (older / "batch.json").write_text(json.dumps({"batch_id": "batch-a", "started_at": "2026-10-28T10:00:00+00:00"}), encoding="utf-8")
    (older / "run_index.jsonl").write_text(json.dumps({"scenario_id": "s1", "failure_kind": "passed"}) + "\n", encoding="utf-8")
    (older / "tasks.jsonl").write_bytes(b'{"scenario_id": "s1"}\r\n')
    newer = _batch(tmp_path, "batch-b", "2026-10-29T11:00:00+00:00", [("s1", "passed")])
    meta = json.loads((newer / "batch.json").read_text(encoding="utf-8"))
    meta["provenance"]["tasks_sha256"] = script.tasks_sha256(older / "tasks.jsonl")
    (newer / "batch.json").write_text(json.dumps(meta), encoding="utf-8")

    assert _run(tmp_path, "batch-a", "batch-b") == 0
