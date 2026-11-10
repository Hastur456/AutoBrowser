"""scripts/ci_regression_mode.py: which live regression mode a pull request event asks for.

The whole table of design §5.3: only a label change that flips "requested" writes a status,
an unrelated label keeps the last result (R7), ``REGRESSION_DEFAULT`` and ``regression:skip``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import ci_regression_mode as script

BEFORE, AFTER, SKIP = "regression:before-merge", "regression:after-merge", "regression:skip"


def _event(action: str, labels: list[str], *, label: str | None = None, merged: bool = False, fork: bool = False) -> dict[str, Any]:
    head_repo = "someone/fork" if fork else "owner/repo"
    event: dict[str, Any] = {
        "action": action,
        "pull_request": {
            "number": 7,
            "merged": merged,
            "labels": [{"name": name} for name in labels],
            "head": {"sha": "headsha", "repo": {"full_name": head_repo}},
            "base": {"repo": {"full_name": "owner/repo"}},
        },
    }
    if label is not None:
        event["label"] = {"name": label}
    return event


def _decide(event: dict[str, Any], default: str = "off") -> script.Decision:
    return script.decide(event, default)


@pytest.mark.parametrize("action", ["opened", "reopened", "synchronize"])
def test_a_new_head_without_a_label_passes_and_with_it_runs(action: str) -> None:
    assert _decide(_event(action, [])).before == "pass"
    decision = _decide(_event(action, [BEFORE]))
    assert decision.before == "run"
    assert decision.after == "none"
    assert decision.status_sha == "headsha"


def test_adding_the_label_starts_a_run() -> None:
    assert _decide(_event("labeled", [BEFORE], label=BEFORE)).before == "run"


def test_removing_the_label_passes_with_a_removed_reason() -> None:
    decision = _decide(_event("unlabeled", [], label=BEFORE))

    assert decision.before == "pass"
    assert "removed" in decision.reason


@pytest.mark.parametrize(
    ("action", "labels", "label"),
    [
        ("labeled", [BEFORE, "docs"], "docs"),  # an unrelated label next to a requested run
        ("unlabeled", [BEFORE], "docs"),
        ("labeled", ["docs"], "docs"),  # nothing requested before or after
        ("labeled", [BEFORE, AFTER], AFTER),  # the after-merge label changes nothing before merge
    ],
)
def test_a_label_that_changes_nothing_keeps_the_last_status(action: str, labels: list[str], label: str) -> None:
    assert _decide(_event(action, labels, label=label)).before == "keep"


def test_other_actions_keep_the_status() -> None:
    assert _decide(_event("edited", [BEFORE])).before == "keep"


@pytest.mark.parametrize(
    ("default", "labels", "before", "after"),
    [
        ("off", [], False, False),
        ("before", [], True, False),
        ("after", [], False, True),
        ("both", [], True, True),
        ("both", [SKIP], False, False),
        ("both", [SKIP, BEFORE], True, False),  # an explicit label beats the skip
        ("off", [BEFORE, AFTER], True, True),
    ],
)
def test_requested_follows_the_labels_and_the_default(default: str, labels: list[str], before: bool, after: bool) -> None:
    assert script.requested(set(labels), default) == (before, after)


def test_the_skip_label_cancels_a_default_run() -> None:
    decision = _decide(_event("labeled", [SKIP], label=SKIP), default="before")

    assert decision.before == "pass"


def test_a_merge_with_the_after_label_runs_after_merge_only() -> None:
    decision = _decide(_event("closed", [AFTER], merged=True))

    assert decision.after == "run"
    assert decision.before == "keep"


@pytest.mark.parametrize(
    ("labels", "merged", "default"),
    [([], True, "off"), ([AFTER], False, "off"), ([SKIP], True, "after")],
)
def test_no_after_merge_run_without_a_request_or_a_merge(labels: list[str], merged: bool, default: str) -> None:
    assert _decide(_event("closed", labels, merged=merged), default=default).after == "none"


def test_a_default_after_merge_runs_on_merge() -> None:
    assert _decide(_event("closed", [], merged=True), default="after").after == "run"


def test_a_fork_pull_request_writes_nothing_and_runs_nothing() -> None:
    opened = _decide(_event("opened", [BEFORE], fork=True))
    merged = _decide(_event("closed", [AFTER], merged=True, fork=True))

    assert (opened.before, opened.after) == ("keep", "none")
    assert (merged.before, merged.after) == ("keep", "none")
    assert "fork" in opened.reason


def test_an_unknown_default_is_off_with_a_warning(capsys: pytest.CaptureFixture[str]) -> None:
    assert script.normalize_default("sometimes") == "off"
    assert "REGRESSION_DEFAULT" in capsys.readouterr().err


def test_main_prints_github_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(_event("labeled", [BEFORE], label=BEFORE)), encoding="utf-8")
    monkeypatch.setenv("REGRESSION_DEFAULT", "off")

    assert script.main(["--event", str(path)]) == 0

    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert lines["before"] == "run"
    assert lines["after"] == "none"
    assert lines["status_sha"] == "headsha"
    assert lines["reason"] and "\n" not in lines["reason"]


def test_the_script_imports_only_the_standard_library() -> None:
    source = Path(script.__file__).read_text(encoding="utf-8")

    assert "from src" not in source and "import src" not in source
    assert "from scripts" not in source
