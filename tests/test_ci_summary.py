"""scripts/ci_summary.py: gate summary, live verdict (design §5.7), status, comments, issues.

GitHub is a fake: no network, every write is recorded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import ci_state
from scripts import ci_summary as script
from src.config import get_settings

MARKER = script.STICKY_MARKER


class FakeGitHub:
    def __init__(self, *, head: str = "headsha", labels: tuple[str, ...] = ("regression:before-merge",)) -> None:
        self.head = head
        self.labels = list(labels)
        self.statuses: list[tuple[str, str, str, str]] = []
        self.sticky: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.issues: dict[int, dict[str, Any]] = {}
        self.issue_comments: list[tuple[int, str]] = []

    def pull(self, pr: str) -> dict[str, Any]:
        return {"head": {"sha": self.head}, "labels": [{"name": name} for name in self.labels]}

    def write_status(self, sha: str, state: str, description: str, target_url: str) -> None:
        self.statuses.append((sha, state, description, target_url))

    def upsert_sticky_comment(self, pr: str, body: str) -> None:
        self.sticky.append((pr, body))

    def post_comment(self, pr: str, body: str) -> None:
        self.comments.append((pr, body))

    def open_issues(self, label: str) -> list[int]:
        return [n for n, issue in self.issues.items() if issue["label"] == label and issue["open"]]

    def create_issue(self, title: str, body: str, label: str) -> None:
        self.issues[len(self.issues) + 1] = {"title": title, "body": body, "label": label, "open": True}

    def comment_issue(self, number: int, body: str) -> None:
        self.issue_comments.append((number, body))

    def close_issue(self, number: int) -> None:
        self.issues[number]["open"] = False


def _report(
    status: str = "compared",
    exit_code: int = 0,
    *,
    counts: dict[str, int] | None = None,
    findings: list[dict[str, Any]] | None = None,
    batch_dir: str = "",
) -> dict[str, Any]:
    counts = counts or {"total": 10, "passed": 10, "agent": 0, "infra": 0, "ungraded": 0}
    return {
        "status": status,
        "exit_code": exit_code,
        "batch": {"batch_id": "batch-new", "dir": batch_dir, "commit": "c" * 40, "counts": counts},
        "base": None
        if status == "no_base"
        else {"batch_id": "batch-old", "commit": "b" * 40, "pr": "17", "counts": counts},
        "comparison": None
        if status == "no_base"
        else {"findings": findings or [], "warnings": [], "unchanged": 10, "exit_code": exit_code},
    }


def _write(out: Path, report: dict[str, Any] | None) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    if report is not None:
        (out / "compare.json").write_text(json.dumps(report), encoding="utf-8")
    return out


def _live(tmp_path: Path, report: dict[str, Any] | None, mode: str, github: FakeGitHub, **kwargs: Any) -> int:
    out = _write(tmp_path / "ci-out", report)
    return script.run_live(
        mode=mode,
        out=out,
        status_sha=kwargs.get("status_sha", "headsha"),
        pr=kwargs.get("pr", "7"),
        suite="benchmark",
        commit=kwargs.get("commit", "c" * 40),
        github=github,
        summary_path=tmp_path / "summary.md",
        default=kwargs.get("default", "off"),
    )


REGRESSION = {"kind": "regression", "scenario_id": "s1", "before": "passed", "after": "guard_limit", "metric": "", "change": None}
SLOWER = {"kind": "slower", "scenario_id": "s2", "before": 1000, "after": 2000, "metric": "duration_ms", "change": 1.0}


# --------------------------------------------------------------------------
# the verdict table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("report", "kind"),
    [
        (_report("compared", 0), "pass"),
        (_report("compared", 1, findings=[REGRESSION]), "regression"),
        (_report("no_base", 2), "no_base"),
        (_report("compared", 3, findings=[SLOWER]), "perf"),
        (None, "infra"),
        ({"status": "no_batch", "exit_code": 2, "error": "boom", "batch": None, "base": None, "comparison": None}, "infra"),
        (_report("compared", 0, counts={"total": 0, "passed": 0, "agent": 0, "infra": 0, "ungraded": 0}), "infra"),
        # 4 of 10 infra failures is past the default 0.3: the regression is not trusted.
        (_report("compared", 1, findings=[REGRESSION], counts={"total": 10, "passed": 5, "agent": 1, "infra": 4, "ungraded": 0}), "infra"),
        (_report("compared", 0, counts={"total": 10, "passed": 7, "agent": 0, "infra": 3, "ungraded": 0}), "pass"),
    ],
)
def test_the_verdict_follows_the_table(report: dict[str, Any] | None, kind: str) -> None:
    assert script.live_verdict(report, get_settings().pipeline.ci_max_inconclusive_ratio).kind == kind


# --------------------------------------------------------------------------
# before merge: status and sticky comment, rechecked against the PR
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("report", "state", "code"),
    [
        (_report("compared", 0), "success", 0),
        (_report("compared", 1, findings=[REGRESSION]), "failure", 1),
        (_report("no_base", 2), "success", 0),
        (_report("compared", 3, findings=[SLOWER]), "success", 0),
        (None, "error", 2),
    ],
)
def test_before_merge_writes_the_status_and_one_sticky_comment(
    tmp_path: Path, report: dict[str, Any] | None, state: str, code: int
) -> None:
    github = FakeGitHub()

    assert _live(tmp_path, report, "before-merge", github) == code

    assert [(s[0], s[1]) for s in github.statuses] == [("headsha", state)]
    assert all(len(s[2]) <= script.STATUS_DESCRIPTION_LIMIT for s in github.statuses)
    assert len(github.sticky) == 1 and MARKER in github.sticky[0][1]
    assert not github.issues


def test_the_regression_comment_names_the_scenarios_and_the_base(tmp_path: Path) -> None:
    github = FakeGitHub()

    _live(tmp_path, _report("compared", 1, findings=[REGRESSION, SLOWER]), "before-merge", github)

    body = github.sticky[0][1]
    assert "s1" in body and "guard_limit" in body
    assert "batch-old" in body and "#17" in body
    assert "s2" in body  # the perf finding is a warning in the comment
    assert "s1" in github.statuses[0][2]
    assert "s1" in (tmp_path / "summary.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "github",
    [
        FakeGitHub(head="newer"),  # a new push: the next run writes its own status
        FakeGitHub(labels=()),  # the label was removed while the run went on
    ],
)
def test_a_superseded_run_does_not_overwrite_the_status(tmp_path: Path, github: FakeGitHub) -> None:
    code = _live(tmp_path, _report("compared", 1, findings=[REGRESSION]), "before-merge", github)

    assert code == 1
    assert github.statuses == []
    assert github.sticky == []


def test_a_default_request_still_counts_on_recheck(tmp_path: Path) -> None:
    github = FakeGitHub(labels=())

    _live(tmp_path, _report("compared", 0), "before-merge", github, default="before")

    assert [s[1] for s in github.statuses] == ["success"]


def test_a_finished_report_leaves_a_marker_for_the_fallback_status(tmp_path: Path) -> None:
    _live(tmp_path, _report("compared", 1, findings=[REGRESSION]), "before-merge", FakeGitHub())

    assert (tmp_path / "ci-out" / script.DONE_FILE_NAME).read_text(encoding="utf-8") == "regression"


def test_before_merge_never_writes_the_verdict_journal(tmp_path: Path) -> None:
    _live(tmp_path, _report("compared", 0), "before-merge", FakeGitHub())

    assert ci_state.read_verdicts(get_settings().storage.root_dir) == []


# --------------------------------------------------------------------------
# after merge and manual: journal, PR comment, issues
# --------------------------------------------------------------------------


def test_after_merge_journals_comments_and_opens_one_issue_per_regression_streak(tmp_path: Path) -> None:
    github = FakeGitHub()
    report = _report("compared", 1, findings=[REGRESSION])

    assert _live(tmp_path, report, "after-merge", github) == 1
    assert _live(tmp_path, report, "after-merge", github) == 1

    rows = ci_state.read_verdicts(get_settings().storage.root_dir)
    assert [row["verdict"] for row in rows] == ["regression", "regression"]
    assert rows[0]["regressions"] == ["s1"]
    assert rows[0]["pr"] == "7" and rows[0]["batch_id"] == "batch-new"
    assert len(github.comments) == 2 and github.statuses == []
    assert len(github.issues) == 1  # the second run adds to the open issue
    assert github.issues[1]["label"] == script.REGRESSION_ISSUE_LABEL
    assert len(github.issue_comments) == 1


def test_a_green_measurement_closes_the_open_issues(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.create_issue("r", "b", script.REGRESSION_ISSUE_LABEL)
    github.create_issue("i", "b", script.INFRA_ISSUE_LABEL)

    assert _live(tmp_path, _report("compared", 0), "after-merge", github) == 0

    assert github.open_issues(script.REGRESSION_ISSUE_LABEL) == []
    assert github.open_issues(script.INFRA_ISSUE_LABEL) == []


def test_an_infra_measurement_opens_the_infra_issue_and_keeps_the_regression_one(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.create_issue("r", "b", script.REGRESSION_ISSUE_LABEL)

    assert _live(tmp_path, None, "manual", github, pr="") == 2

    assert github.open_issues(script.REGRESSION_ISSUE_LABEL) == [1]
    assert github.open_issues(script.INFRA_ISSUE_LABEL) == [2]
    assert github.comments == []  # a manual run has no PR
    assert ci_state.read_verdicts(get_settings().storage.root_dir)[0]["verdict"] == "infra"


def test_the_langfuse_experiment_link_reaches_the_comment_and_the_journal(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch-new"
    batch_dir.mkdir()
    (batch_dir / "platform_sync.json").write_text(
        json.dumps({"experiment": {"url": "https://lf.example/exp/1"}}), encoding="utf-8"
    )
    github = FakeGitHub()

    _live(tmp_path, _report("compared", 0, batch_dir=str(batch_dir)), "after-merge", github)

    assert "https://lf.example/exp/1" in github.comments[0][1]
    assert ci_state.read_verdicts(get_settings().storage.root_dir)[0]["experiment_url"] == "https://lf.example/exp/1"


def test_a_github_failure_is_a_warning_not_a_crash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class Broken(FakeGitHub):
        def post_comment(self, pr: str, body: str) -> None:
            raise script.GitHubError("HTTP 502")

    assert _live(tmp_path, _report("compared", 0), "after-merge", Broken()) == 0
    assert "HTTP 502" in capsys.readouterr().err


# --------------------------------------------------------------------------
# the gh adapter
# --------------------------------------------------------------------------


def test_the_sticky_comment_is_edited_when_it_exists_and_created_otherwise() -> None:
    calls: list[tuple[list[str], str | None]] = []

    def gh(argv: list[str], stdin: str | None = None) -> str:
        calls.append((argv, stdin))
        if "--paginate" in argv:
            return "11\n42\n" if existing else ""
        return "{}"

    github = script.GhGitHub("owner/repo", gh)
    existing = True
    github.upsert_sticky_comment("7", f"{MARKER}\nbody")
    existing = False
    github.upsert_sticky_comment("7", f"{MARKER}\nbody")

    writes = [(argv, stdin) for argv, stdin in calls if "--paginate" not in argv]
    assert "repos/owner/repo/issues/comments/42" in writes[0][0]
    assert "PATCH" in writes[0][0]
    assert "repos/owner/repo/issues/7/comments" in writes[1][0]
    assert json.loads(writes[1][1] or "{}")["body"].startswith(MARKER)


def test_the_status_goes_to_the_statuses_api() -> None:
    calls: list[tuple[list[str], str | None]] = []
    github = script.GhGitHub("owner/repo", lambda argv, stdin=None: calls.append((argv, stdin)) or "{}")

    github.write_status("abc", "failure", "1 regression: s1", "https://run")

    argv, stdin = calls[0]
    assert "repos/owner/repo/statuses/abc" in argv
    assert json.loads(stdin or "{}") == {
        "state": "failure",
        "context": "regression/before-merge",
        "description": "1 regression: s1",
        "target_url": "https://run",
    }


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="2" tests="5" time="12.5">
<testcase classname="tests.test_a" name="test_ok" time="0.1"/>
<testcase classname="tests.test_a" name="test_bad" time="0.1"><failure message="assert 1 == 2">long</failure></testcase>
</testsuite></testsuites>
"""


def test_the_gate_summary_has_pytest_the_evals_and_the_failed_tests(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(JUNIT, encoding="utf-8")
    log = tmp_path / "evals-agent_loop_v2.txt"
    log.write_text("agent_loop_v2: regression: scenario x\n", encoding="utf-8")
    summary = tmp_path / "summary.md"

    code = script.main(
        [
            "gate",
            "--junit",
            str(junit),
            "--eval",
            f"agent_loop_v2=failure={log}",
            "--eval",
            f"playback_v1=success={tmp_path / 'missing.txt'}",
            "--summary",
            str(summary),
        ]
    )

    text = summary.read_text(encoding="utf-8")
    assert code == 0
    assert "2 passed, 1 failed, 0 errors, 2 skipped" in text
    assert "tests.test_a::test_bad" in text and "assert 1 == 2" in text
    assert "agent_loop_v2" in text and "failure" in text and "regression: scenario x" in text
    assert "playback_v1" in text and "success" in text


def test_the_gate_summary_without_a_junit_report_says_so(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"

    script.main(["gate", "--junit", str(tmp_path / "none.xml"), "--summary", str(summary)])

    assert "no report" in summary.read_text(encoding="utf-8")
