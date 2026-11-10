"""scripts/ci_state.py: the CI state chain, the nearest measured ancestor, the release verdict.

Ancestry runs against a real temporary git repository with a side branch; GitHub is a fake
``gh`` runner. Nothing here touches the network.
"""

from __future__ import annotations

import json
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import ci_state as script
from src.config import get_settings


def _git(repo: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv], cwd=repo, capture_output=True, encoding="utf-8", check=True
    )
    return completed.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / f"{name}.txt").write_text(name, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Any]:
    """main: a -> b -> d; side: a -> x (never merged). Returns the path and the commits."""

    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "ci@example.invalid")
    _git(path, "config", "user.name", "ci")
    _git(path, "config", "commit.gpgsign", "false")
    a = _commit(path, "a")
    b = _commit(path, "b")
    _git(path, "checkout", "-q", "-b", "side", a)
    x = _commit(path, "x")
    _git(path, "checkout", "-q", "main")
    d = _commit(path, "d")
    return {"path": path, "a": a, "b": b, "x": x, "d": d}


def _batch(
    batches: Path,
    batch_id: str,
    started_at: str,
    commit: str | None,
    rows: list[tuple[str, str]],
    *,
    tasks: str = "t",
    finished: bool = True,
) -> Path:
    batch_dir = batches / batch_id
    batch_dir.mkdir(parents=True)
    meta = {
        "schema_version": 1,
        "batch_id": batch_id,
        "started_at": started_at,
        "finished_at": started_at if finished else None,
        "config": {},
        "provenance": {
            "tasks_sha256": tasks,
            "settings_sha256": "s",
            "model": "m",
            "git": {"commit": commit, "branch": "main", "dirty": False},
        },
    }
    (batch_dir / "batch.json").write_text(json.dumps(meta), encoding="utf-8")
    lines = [
        json.dumps({"scenario_id": s, "failure_kind": k, "metrics": {"duration_ms": 1000}})
        for s, k in rows
    ]
    (batch_dir / "run_index.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return batch_dir


def _batch_json(tmp_path: Path, batch_dir: Path) -> Path:
    path = tmp_path / "batch-result.json"
    path.write_text(
        json.dumps({"batch_id": batch_dir.name, "batch_dir": str(batch_dir), "status": "completed"}),
        encoding="utf-8",
    )
    return path


def _compare(tmp_path: Path, repo: dict[str, Any], new_dir: Path) -> tuple[int, dict[str, Any]]:
    out = tmp_path / "ci-out"
    code = script.main(
        [
            "compare",
            "--batch-json",
            str(_batch_json(tmp_path, new_dir)),
            "--out",
            str(out),
            "--repo",
            str(repo["path"]),
        ]
    )
    return code, json.loads((out / "compare.json").read_text(encoding="utf-8"))


def _batches_dir() -> Path:
    return get_settings().storage.batches_dir


# --------------------------------------------------------------------------
# compare: the nearest measured ancestor is the base
# --------------------------------------------------------------------------


def test_the_base_is_the_nearest_measured_ancestor_not_the_newest_run(
    tmp_path: Path, repo: dict[str, Any]
) -> None:
    batches = _batches_dir()
    _batch(batches, "batch-a", "2026-11-01T10:00:00+00:00", repo["a"], [("s1", "passed")])
    _batch(batches, "batch-b", "2026-11-02T10:00:00+00:00", repo["b"], [("s1", "passed")])
    # Measured later but on a side branch that never reached main: never a base.
    _batch(batches, "batch-x", "2026-11-03T10:00:00+00:00", repo["x"], [("s1", "passed")])
    new = _batch(batches, "batch-d", "2026-11-04T10:00:00+00:00", repo["d"], [("s1", "passed")])

    code, report = _compare(tmp_path, repo, new)

    assert code == 0
    assert report["status"] == "compared"
    assert report["base"]["batch_id"] == "batch-b"
    assert report["base"]["commit"] == repo["b"]
    assert report["batch"]["counts"] == {"total": 1, "passed": 1, "agent": 0, "infra": 0, "ungraded": 0}


def test_a_late_measurement_of_an_old_commit_does_not_become_the_base(
    tmp_path: Path, repo: dict[str, Any]
) -> None:
    batches = _batches_dir()
    _batch(batches, "batch-b", "2026-11-02T10:00:00+00:00", repo["b"], [("s1", "passed")])
    # A manual run of the older commit a, started after the run of b.
    _batch(batches, "batch-a", "2026-11-03T10:00:00+00:00", repo["a"], [("s1", "passed")])
    new = _batch(batches, "batch-d", "2026-11-04T10:00:00+00:00", repo["d"], [("s1", "passed")])

    _, report = _compare(tmp_path, repo, new)

    assert report["base"]["batch_id"] == "batch-b"


def test_another_golden_set_and_unfinished_runs_are_never_a_base(
    tmp_path: Path, repo: dict[str, Any]
) -> None:
    batches = _batches_dir()
    _batch(batches, "batch-a", "2026-11-01T10:00:00+00:00", repo["a"], [("s1", "passed")])
    _batch(batches, "batch-b", "2026-11-02T10:00:00+00:00", repo["b"], [("s1", "passed")], tasks="other")
    _batch(batches, "batch-b2", "2026-11-02T11:00:00+00:00", repo["b"], [("s1", "passed")], finished=False)
    new = _batch(batches, "batch-d", "2026-11-04T10:00:00+00:00", repo["d"], [("s1", "passed")])

    _, report = _compare(tmp_path, repo, new)

    assert report["base"]["batch_id"] == "batch-a"


def test_a_regression_against_the_base_exits_1_and_names_the_scenario(
    tmp_path: Path, repo: dict[str, Any]
) -> None:
    batches = _batches_dir()
    _batch(batches, "batch-b", "2026-11-02T10:00:00+00:00", repo["b"], [("s1", "passed"), ("s2", "passed")])
    new = _batch(
        batches,
        "batch-d",
        "2026-11-04T10:00:00+00:00",
        repo["d"],
        [("s1", "guard_limit"), ("s2", "timeout")],
    )

    code, report = _compare(tmp_path, repo, new)

    assert code == 1
    kinds = {f["scenario_id"]: f["kind"] for f in report["comparison"]["findings"]}
    assert kinds == {"s1": "regression", "s2": "inconclusive"}
    assert report["batch"]["counts"]["agent"] == 1
    assert report["batch"]["counts"]["infra"] == 1
    assert (new / "compare.md").is_file()


def test_the_base_pr_comes_from_the_verdict_journal(tmp_path: Path, repo: dict[str, Any]) -> None:
    batches = _batches_dir()
    _batch(batches, "batch-b", "2026-11-02T10:00:00+00:00", repo["b"], [("s1", "passed")])
    script.append_verdict(get_settings().storage.root_dir, {"batch_id": "batch-b", "pr": "17", "verdict": "pass"})
    new = _batch(batches, "batch-d", "2026-11-04T10:00:00+00:00", repo["d"], [("s1", "passed")])

    _, report = _compare(tmp_path, repo, new)

    assert report["base"]["pr"] == "17"


def test_no_measured_ancestor_is_no_base_and_exits_2(tmp_path: Path, repo: dict[str, Any]) -> None:
    batches = _batches_dir()
    _batch(batches, "batch-x", "2026-11-03T10:00:00+00:00", repo["x"], [("s1", "passed")])
    new = _batch(batches, "batch-d", "2026-11-04T10:00:00+00:00", repo["d"], [("s1", "passed")])

    code, report = _compare(tmp_path, repo, new)

    assert code == 2
    assert report["status"] == "no_base"
    assert report["base"] is None


def test_an_unreadable_batch_result_is_no_batch(tmp_path: Path, repo: dict[str, Any]) -> None:
    broken = tmp_path / "batch-result.json"
    broken.write_text("", encoding="utf-8")
    out = tmp_path / "ci-out"

    code = script.main(
        ["compare", "--batch-json", str(broken), "--out", str(out), "--repo", str(repo["path"])]
    )

    report = json.loads((out / "compare.json").read_text(encoding="utf-8"))
    assert code == 2
    assert report["status"] == "no_batch"
    assert report["error"]


# --------------------------------------------------------------------------
# save / restore: the artifact chain
# --------------------------------------------------------------------------


def test_save_keeps_the_newest_batches_without_profiles_and_the_journals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: dict[str, Any]
) -> None:
    monkeypatch.setenv("AUTOBROWSER_PIPELINE__CI_STATE_KEEP_BATCHES", "2")
    get_settings.cache_clear()
    root = get_settings().storage.root_dir
    batches = _batches_dir()
    for day in (1, 2, 3):
        batch_dir = _batch(batches, f"batch-{day}", f"2026-11-0{day}T10:00:00+00:00", repo["a"], [])
        (batch_dir / "profiles" / "scenario-1").mkdir(parents=True)
        (batch_dir / "profiles" / "scenario-1" / "Cookies").write_text("secret", encoding="utf-8")
        (batch_dir / "traces").mkdir()
        (batch_dir / "traces" / "s1.jsonl").write_text("{}", encoding="utf-8")
    (batches / "history.jsonl").write_text("{}\n", encoding="utf-8")
    (batches / "platform_datasets.jsonl").write_text("{}\n", encoding="utf-8")
    script.append_verdict(root, {"batch_id": "batch-3", "verdict": "pass"})
    archive = tmp_path / "ci-state.tar.gz"

    assert script.main(["save", "--out", str(archive)]) == 0

    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert "batches/batch-1/batch.json" not in names
    assert {"batches/batch-2/batch.json", "batches/batch-3/batch.json"} <= names
    assert "batches/batch-3/traces/s1.jsonl" in names
    assert not any("/profiles" in name for name in names)
    assert {"batches/history.jsonl", "batches/platform_datasets.jsonl", "ci/verdicts.jsonl"} <= names


def test_restore_unpacks_the_newest_live_artifact_into_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: dict[str, Any]
) -> None:
    root = get_settings().storage.root_dir
    _batch(_batches_dir(), "batch-1", "2026-11-01T10:00:00+00:00", repo["a"], [])
    archive = tmp_path / "packed.tar.gz"
    assert script.main(["save", "--out", str(archive)]) == 0
    for path in sorted(root.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    calls: list[list[str]] = []

    def gh(argv: list[str]) -> str:
        calls.append(argv)
        if argv[0] == "api":
            return json.dumps(
                {
                    "artifacts": [
                        {"expired": True, "created_at": "2026-11-09T00:00:00Z", "workflow_run": {"id": 9}},
                        {"expired": False, "created_at": "2026-11-05T00:00:00Z", "workflow_run": {"id": 5}},
                        {"expired": False, "created_at": "2026-11-07T00:00:00Z", "workflow_run": {"id": 7}},
                    ]
                }
            )
        target = Path(argv[argv.index("-D") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "ci-state.tar.gz").write_bytes(archive.read_bytes())
        return ""

    assert script.restore(root, gh) is True

    assert calls[1][:3] == ["run", "download", "7"]
    assert (root / "batches" / "batch-1" / "batch.json").is_file()


def test_restore_without_an_artifact_is_a_clean_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    restored = script.restore(get_settings().storage.root_dir, lambda argv: json.dumps({"artifacts": []}))

    assert restored is False
    assert "no ci-state" in capsys.readouterr().out


def test_restore_rejects_an_archive_escaping_the_home(tmp_path: Path) -> None:
    evil = tmp_path / "evil.tar.gz"
    payload = tmp_path / "payload.txt"
    payload.write_text("x", encoding="utf-8")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(payload, arcname="../outside.txt")

    with pytest.raises(tarfile.TarError):
        script.unpack_state(evil, get_settings().storage.root_dir)
    assert not (get_settings().storage.root_dir.parent / "outside.txt").exists()


# --------------------------------------------------------------------------
# verdict (release) and check-ref
# --------------------------------------------------------------------------

_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _row(commit: str, verdict: str, age_days: float) -> dict[str, Any]:
    return {
        "commit": commit,
        "verdict": verdict,
        "finished_at": (_NOW - timedelta(days=age_days)).isoformat(),
    }


def test_a_recent_green_measurement_of_an_ancestor_releases(repo: dict[str, Any]) -> None:
    rows = [_row(repo["a"], "regression", 1), _row(repo["b"], "pass", 2)]

    found = script.green_verdict(rows, repo["d"], script.git_ancestry(repo["path"]), now=_NOW, max_age_days=14)

    assert found is not None and found["commit"] == repo["b"]


@pytest.mark.parametrize(
    "row_of",
    [
        lambda r: _row(r["b"], "pass", 20),  # too old
        lambda r: _row(r["b"], "regression", 1),  # red
        lambda r: _row(r["b"], "infra", 1),  # inconclusive
        lambda r: _row(r["x"], "pass", 1),  # not an ancestor
    ],
)
def test_no_release_without_a_recent_green_ancestor(repo: dict[str, Any], row_of: Any) -> None:
    found = script.green_verdict(
        [row_of(repo)], repo["d"], script.git_ancestry(repo["path"]), now=_NOW, max_age_days=14
    )

    assert found is None


def test_the_verdict_command_reads_the_journal_and_says_what_to_do(
    repo: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    root = get_settings().storage.root_dir
    script.append_verdict(root, {**_row(repo["b"], "regression", 0), "finished_at": datetime.now(UTC).isoformat()})

    code = script.main(["verdict", "--commit", repo["d"], "--repo", str(repo["path"])])

    assert code == 1
    assert "regression" in capsys.readouterr().err

    script.append_verdict(root, {**_row(repo["b"], "perf", 0), "finished_at": datetime.now(UTC).isoformat()})

    assert script.main(["verdict", "--commit", repo["d"], "--repo", str(repo["path"])]) == 0


def test_check_ref_requires_main_only_for_main_measurements(repo: dict[str, Any]) -> None:
    path = str(repo["path"])
    _git(repo["path"], "checkout", "-q", "side")

    assert script.main(["check-ref", "--mode", "before-merge", "--repo", path, "--branch", "main"]) == 0
    assert script.main(["check-ref", "--mode", "after-merge", "--repo", path, "--branch", "main"]) == 1

    _git(repo["path"], "checkout", "-q", repo["b"])

    assert script.main(["check-ref", "--mode", "manual", "--repo", path, "--branch", "main"]) == 0


def test_the_tasks_command_prints_the_suite_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(["tasks", "--suite", "golden"]) == 0
    assert capsys.readouterr().out.strip() == "tests/golden/tasks.jsonl"
    assert script.main(["tasks", "--suite", "nope"]) == 2


# --------------------------------------------------------------------------
# review fixes: no infra base, pruning per Golden Set
# --------------------------------------------------------------------------


def test_an_infra_measurement_is_never_a_base(tmp_path: Path, repo: dict[str, Any]) -> None:
    batches = _batches_dir()
    root = get_settings().storage.root_dir
    _batch(batches, "batch-a", "2026-11-01T10:00:00+00:00", repo["a"], [("s1", "passed"), ("s2", "passed")])
    # The provider went down during the measurement of b: journalled as infra.
    _batch(batches, "batch-b", "2026-11-02T10:00:00+00:00", repo["b"], [("s1", "provider_error"), ("s2", "passed")])
    script.append_verdict(root, {"batch_id": "batch-b", "verdict": "infra"})
    new = _batch(batches, "batch-d", "2026-11-04T10:00:00+00:00", repo["d"], [("s1", "guard_limit"), ("s2", "passed")])

    code, report = _compare(tmp_path, repo, new)

    assert report["base"]["batch_id"] == "batch-a"
    assert code == 1


def test_a_batch_past_the_infra_share_is_never_a_base_even_without_a_verdict(
    tmp_path: Path, repo: dict[str, Any]
) -> None:
    batches = _batches_dir()
    _batch(batches, "batch-a", "2026-11-01T10:00:00+00:00", repo["a"], [("s1", "passed"), ("s2", "passed")])
    _batch(batches, "batch-b", "2026-11-02T10:00:00+00:00", repo["b"], [("s1", "timeout"), ("s2", "provider_error")])
    new = _batch(batches, "batch-d", "2026-11-04T10:00:00+00:00", repo["d"], [("s1", "guard_limit"), ("s2", "passed")])

    _, report = _compare(tmp_path, repo, new)

    assert report["base"]["batch_id"] == "batch-a"


def test_save_keeps_the_newest_batches_of_every_golden_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: dict[str, Any]
) -> None:
    monkeypatch.setenv("AUTOBROWSER_PIPELINE__CI_STATE_KEEP_BATCHES", "2")
    get_settings.cache_clear()
    batches = _batches_dir()
    _batch(batches, "batch-bench", "2026-11-01T10:00:00+00:00", repo["a"], [], tasks="benchmark")
    for day in (2, 3, 4):
        _batch(batches, f"batch-smoke-{day}", f"2026-11-0{day}T10:00:00+00:00", repo["a"], [], tasks="smoke")
    archive = tmp_path / "ci-state.tar.gz"

    assert script.main(["save", "--out", str(archive)]) == 0

    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert "batches/batch-bench/batch.json" in names
    assert "batches/batch-smoke-2/batch.json" not in names
    assert {"batches/batch-smoke-3/batch.json", "batches/batch-smoke-4/batch.json"} <= names
