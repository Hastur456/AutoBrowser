#!/usr/bin/env python3
"""Reports of the CI runs: the offline gate summary and the live regression verdict.

``gate --junit FILE [--eval NAME=OUTCOME=LOG ...]`` writes the pytest totals, the failed tests
and one line per eval suite to the step summary; it always exits 0 (the steps decide).

``live --mode MODE --out DIR ...`` reads ``DIR/compare.json`` (``scripts/ci_state.py
compare``) and turns it into the verdict of design §5.7: ``pass``, ``regression``,
``no_base``, ``perf`` or ``infra`` (no comparison, no scenario, or more infra failures than
``pipeline.ci_max_inconclusive_ratio``). Then, by mode:

- ``before-merge``: rereads the PR and, only if its head is still ``--status-sha`` and the
  run is still requested, writes the ``regression/before-merge`` status and the sticky PR
  comment; a superseded run writes nothing;
- ``after-merge`` and ``manual``: appends the verdict to ``ci/verdicts.jsonl``, comments the
  merged PR, and opens, extends or closes the ``regression-main`` / ``regression-infra`` issue.

Exit code: 0 green (pass, no base, perf only), 1 regression, 2 infra. A GitHub API failure
is a warning and never changes it. See docs/development/2026-11-10-regression-ci-cd-design.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ci_regression_mode import normalize_default, requested
from scripts.ci_state import COMPARE_JSON_NAME, MAIN_MODES, MODES, append_verdict
from scripts.langfuse_bridge.ledger import LEDGER_FILE_NAME
from src.config import get_settings

STATUS_CONTEXT = "regression/before-merge"
#: GitHub cuts a commit status description at this many characters.
STATUS_DESCRIPTION_LIMIT = 140
#: Hidden marker of the one comment a before-merge run keeps editing.
STICKY_MARKER = "<!-- autobrowser-regression -->"
#: Written into ``--out`` once the reports are done; without it the workflow's fallback step
#: marks the before-merge status as an infra error (the job broke before reporting).
DONE_FILE_NAME = "summary.done"
REGRESSION_ISSUE_LABEL = "regression-main"
INFRA_ISSUE_LABEL = "regression-infra"
GREEN = frozenset({"pass", "no_base", "perf"})
EXIT_CODES = {"pass": 0, "no_base": 0, "perf": 0, "regression": 1, "infra": 2}
STATES = {"pass": "success", "no_base": "success", "perf": "success", "regression": "failure", "infra": "error"}
TITLES = {
    "pass": "no regression",
    "no_base": "no base to compare with",
    "perf": "no regression, slower or costlier",
    "regression": "regression",
    "infra": "infra failure, rerun",
}
_PERF_KINDS = frozenset({"slower", "costlier"})


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


class GitHubError(RuntimeError):
    """A GitHub API call failed."""


class GitHub(Protocol):
    def pull(self, pr: str) -> dict[str, Any]: ...
    def write_status(self, sha: str, state: str, description: str, target_url: str) -> None: ...
    def upsert_sticky_comment(self, pr: str, body: str) -> None: ...
    def post_comment(self, pr: str, body: str) -> None: ...
    def open_issues(self, label: str) -> list[int]: ...
    def create_issue(self, title: str, body: str, label: str) -> None: ...
    def comment_issue(self, number: int, body: str) -> None: ...
    def close_issue(self, number: int) -> None: ...


#: ``gh`` runner: arguments and an optional stdin in, standard output out.
Gh = Callable[..., str]


def run_gh(argv: list[str], stdin: str | None = None) -> str:
    try:
        completed = subprocess.run(
            ["gh", *argv],
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise GitHubError(f"gh {' '.join(argv[:2])}: {detail.strip()}") from exc
    return completed.stdout


class GhGitHub:
    """:class:`GitHub` over ``gh api``; JSON bodies go on stdin, so no text reaches argv."""

    def __init__(self, repository: str, gh: Gh = run_gh) -> None:
        self._repo = repository
        self._gh = gh

    def _write(self, method: str, path: str, payload: Mapping[str, Any]) -> None:
        self._gh(["api", "--method", method, f"repos/{self._repo}/{path}", "--input", "-"], json.dumps(payload))

    def pull(self, pr: str) -> dict[str, Any]:
        return json.loads(self._gh(["api", f"repos/{self._repo}/pulls/{pr}"]))

    def write_status(self, sha: str, state: str, description: str, target_url: str) -> None:
        payload = {"state": state, "context": STATUS_CONTEXT, "description": description, "target_url": target_url}
        self._write("POST", f"statuses/{sha}", payload)

    def upsert_sticky_comment(self, pr: str, body: str) -> None:
        ids = self._gh(
            [
                "api",
                "--paginate",
                f"repos/{self._repo}/issues/{pr}/comments",
                "--jq",
                f'.[] | select(.body | contains("{STICKY_MARKER}")) | .id',
            ]
        ).split()
        if ids:
            self._write("PATCH", f"issues/comments/{ids[-1]}", {"body": body})
        else:
            self.post_comment(pr, body)

    def post_comment(self, pr: str, body: str) -> None:
        self._write("POST", f"issues/{pr}/comments", {"body": body})

    def open_issues(self, label: str) -> list[int]:
        found = json.loads(self._gh(["api", f"repos/{self._repo}/issues?state=open&labels={label}"]))
        return [int(issue["number"]) for issue in found if "pull_request" not in issue]

    def create_issue(self, title: str, body: str, label: str) -> None:
        self._write("POST", "issues", {"title": title, "body": body, "labels": [label]})

    def comment_issue(self, number: int, body: str) -> None:
        self.post_comment(str(number), body)

    def close_issue(self, number: int) -> None:
        self._write("PATCH", f"issues/{number}", {"state": "closed", "state_reason": "completed"})


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    kind: str
    reason: str


def live_verdict(report: Mapping[str, Any] | None, max_inconclusive_ratio: float) -> Verdict:
    """The verdict of design §5.7 from a ``compare.json`` report (``None`` when missing)."""

    if report is None:
        return Verdict("infra", "the run stopped before the comparison (setup, doctor or the batch)")
    if report.get("status") == "no_batch":
        return Verdict("infra", f"no batch result: {report.get('error')}")
    counts = (report.get("batch") or {}).get("counts") or {}
    total = int(counts.get("total") or 0)
    if total == 0:
        return Verdict("infra", "no scenario ran")
    infra = int(counts.get("infra") or 0)
    if infra / total > max_inconclusive_ratio:
        return Verdict("infra", f"{infra} of {total} scenarios ended in an infra failure")
    if report.get("status") == "no_base":
        return Verdict("no_base", "no measured ancestor of this Golden Set: run a main measurement")
    code = report.get("exit_code")
    regressions = _regressions(report)
    if code == 1:
        return Verdict("regression", f"{len(regressions)} regression(s): {', '.join(regressions)}")
    if code == 3:
        return Verdict("perf", "no regression; slower or costlier scenarios, see the comment")
    if code == 0:
        return Verdict("pass", "no regression against the base")
    return Verdict("infra", f"unexpected comparison exit code {code!r}")


def _findings(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list((report.get("comparison") or {}).get("findings") or [])


def _regressions(report: Mapping[str, Any]) -> list[str]:
    return [str(f["scenario_id"]) for f in _findings(report) if f.get("kind") == "regression"]


# --------------------------------------------------------------------------
# live
# --------------------------------------------------------------------------


def run_live(
    *,
    mode: str,
    out: Path,
    status_sha: str,
    pr: str,
    suite: str,
    commit: str,
    github: GitHub,
    summary_path: Path | None,
    default: str,
) -> int:
    settings = get_settings()
    report = _load_report(out / COMPARE_JSON_NAME)
    verdict = live_verdict(report, settings.pipeline.ci_max_inconclusive_ratio)
    batch_dir = str(((report or {}).get("batch") or {}).get("dir") or "")
    experiment = _experiment_url(Path(batch_dir)) if batch_dir else None
    run_url = _run_url()
    body = render_live(mode, suite, commit, verdict, report, run_url, experiment)
    _write_summary(summary_path, body)
    print(f"verdict: {verdict.kind} ({verdict.reason})")

    if mode == "before-merge":
        _report_before_merge(github, pr, status_sha, default, verdict, body, run_url)
    else:
        append_verdict(
            settings.storage.root_dir,
            {
                "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                "run_url": run_url,
                "mode": mode,
                "commit": commit,
                "suite": suite,
                "pr": pr or None,
                "verdict": verdict.kind,
                "compare_exit": (report or {}).get("exit_code"),
                "regressions": _regressions(report or {}),
                "inconclusive": int((((report or {}).get("batch") or {}).get("counts") or {}).get("infra") or 0),
                "batch_id": ((report or {}).get("batch") or {}).get("batch_id"),
                "experiment_url": experiment,
                "finished_at": datetime.now(UTC).isoformat(),
            },
        )
        _report_main(github, pr, commit, verdict, body, run_url)
    out.mkdir(parents=True, exist_ok=True)
    (out / DONE_FILE_NAME).write_text(verdict.kind, encoding="utf-8")
    return EXIT_CODES[verdict.kind]


def _report_before_merge(
    github: GitHub, pr: str, status_sha: str, default: str, verdict: Verdict, body: str, run_url: str
) -> None:
    try:
        current = github.pull(pr)
    except GitHubError as exc:
        _warn(f"could not reread PR #{pr}, the status is left as it is: {exc}")
        return
    head = str((current.get("head") or {}).get("sha") or "")
    labels = {str(label.get("name")) for label in current.get("labels") or []}
    if head != status_sha:
        print(f"superseded: PR #{pr} moved to {head[:12]}; its own run writes the status")
        return
    if not requested(labels, default)[0]:
        print(f"superseded: the before-merge run of PR #{pr} is no longer requested")
        return
    description = _clip(f"{TITLES[verdict.kind]}: {verdict.reason}")
    _attempt(lambda: github.write_status(status_sha, STATES[verdict.kind], description, run_url), "write the status")
    _attempt(lambda: github.upsert_sticky_comment(pr, body), "update the PR comment")


def _report_main(github: GitHub, pr: str, commit: str, verdict: Verdict, body: str, run_url: str) -> None:
    if pr:
        _attempt(lambda: github.post_comment(pr, body), f"comment PR #{pr}")
    short = commit[:12] or "main"
    if verdict.kind == "regression":
        _upsert_issue(github, REGRESSION_ISSUE_LABEL, f"Live regression on main ({short})", body)
    if verdict.kind == "infra":
        _upsert_issue(github, INFRA_ISSUE_LABEL, f"Live regression infra failure on main ({short})", body)
        return
    closing = [INFRA_ISSUE_LABEL] + ([REGRESSION_ISSUE_LABEL] if verdict.kind in GREEN else [])
    for label in closing:
        for number in _attempt(lambda label=label: github.open_issues(label), f"list {label} issues") or []:
            note = f"Measured {TITLES[verdict.kind]} on `{short}`: {run_url or 'this run'}. Closing."
            _attempt(lambda number=number, note=note: github.comment_issue(number, note), f"comment issue #{number}")
            _attempt(lambda number=number: github.close_issue(number), f"close issue #{number}")


def _upsert_issue(github: GitHub, label: str, title: str, body: str) -> None:
    numbers = _attempt(lambda: github.open_issues(label), f"list {label} issues")
    if numbers is None:
        return
    if numbers:
        _attempt(lambda: github.comment_issue(numbers[0], body), f"comment issue #{numbers[0]}")
    else:
        _attempt(lambda: github.create_issue(title, body, label), f"open a {label} issue")


def render_live(
    mode: str,
    suite: str,
    commit: str,
    verdict: Verdict,
    report: Mapping[str, Any] | None,
    run_url: str,
    experiment: str | None,
) -> str:
    report = report or {}
    batch = report.get("batch") or {}
    base = report.get("base") or {}
    counts = batch.get("counts") or {}
    lines = [
        STICKY_MARKER,
        f"### Live regression ({mode}): {TITLES[verdict.kind]}",
        "",
        f"- Verdict: **{verdict.kind}** — {verdict.reason}",
        f"- Suite: `{suite}` · commit `{commit[:12] or '-'}`" + (f" · [run]({run_url})" if run_url else ""),
    ]
    if base:
        pr = f" (PR #{base['pr']})" if base.get("pr") else ""
        lines.append(f"- Base: `{base.get('batch_id')}` on `{str(base.get('commit') or '')[:12]}`{pr}")
    else:
        lines.append("- Base: none")
    if counts:
        lines.append(
            f"- Scenarios: {counts.get('total', 0)} · passed {counts.get('passed', 0)}"
            f" · agent failures {counts.get('agent', 0)} · infra {counts.get('infra', 0)}"
        )
    if batch.get("batch_id"):
        lines.append(f"- Batch: `{batch['batch_id']}` (artifact of the run)")
    if experiment:
        lines.append(f"- Langfuse experiment: {experiment}")
    findings = [f for f in _findings(report) if f.get("kind") not in _PERF_KINDS]
    perf = [f for f in _findings(report) if f.get("kind") in _PERF_KINDS]
    if findings:
        lines += ["", "| Finding | Scenario | Before | After |", "|---|---|---|---|"]
        lines += [
            f"| {f.get('kind')} | {f.get('scenario_id')} | {_cell(f.get('before'))} | {_cell(f.get('after'))} |"
            for f in sorted(findings, key=lambda f: (f.get("kind") != "regression", str(f.get("scenario_id"))))
        ]
    if perf:
        lines += ["", "Warnings (slower or costlier, not a regression):"]
        lines += [
            f"- {f.get('kind')}: {f.get('scenario_id')} {f.get('metric')} {f.get('before')} -> {f.get('after')}"
            for f in perf
        ]
    return "\n".join(lines) + "\n"


def _load_report(path: Path) -> dict[str, Any] | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def _experiment_url(batch_dir: Path) -> str | None:
    try:
        ledger = json.loads((batch_dir / LEDGER_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    experiment = ledger.get("experiment") if isinstance(ledger, dict) else None
    return (str(experiment.get("url") or "") or None) if isinstance(experiment, dict) else None


def _run_url() -> str:
    server, repo, run = (os.environ.get(name, "") for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else ""


def _attempt(action: Callable[[], Any], what: str) -> Any:
    try:
        return action()
    except GitHubError as exc:
        _warn(f"could not {what}: {exc}")
        return None


def _warn(message: str) -> None:
    # A workflow command: GitHub shows it as an annotation of the step.
    print(f"::warning::{message}", file=sys.stderr)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= STATUS_DESCRIPTION_LIMIT else text[: STATUS_DESCRIPTION_LIMIT - 1] + "…"


def _cell(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|")


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------


def render_gate(junit: Path, evals: Sequence[tuple[str, str, Path]], runner: str) -> str:
    lines = [f"## Offline gate{f' ({runner})' if runner else ''}", "", "| Check | Result |", "|---|---|"]
    failed: list[tuple[str, str]] = []
    try:
        totals, failed = _junit(junit)
        lines.append(f"| pytest | {totals} |")
    except (OSError, ET.ParseError) as exc:
        lines.append(f"| pytest | no report ({type(exc).__name__}): the step did not run or crashed |")
    for name, outcome, _log in evals:
        lines.append(f"| evals `{name}` | {outcome or 'not run'} |")
    if failed:
        lines += ["", "### Failed tests", ""]
        lines += [f"- `{name}` — {message}" for name, message in failed]
    for name, _outcome, log in evals:
        try:
            text = log.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        lines += ["", f"<details><summary>evals {name}</summary>", "", "```text", text, "```", "", "</details>"]
    return "\n".join(lines) + "\n"


def _junit(path: Path) -> tuple[str, list[tuple[str, str]]]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = failures = errors = skipped = 0
    seconds = 0.0
    for suite in suites:
        tests += int(suite.get("tests") or 0)
        failures += int(suite.get("failures") or 0)
        errors += int(suite.get("errors") or 0)
        skipped += int(suite.get("skipped") or 0)
        seconds += float(suite.get("time") or 0)
    failed: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            message = " ".join(str(problem.get("message") or "").split())
            failed.append((f"{case.get('classname')}::{case.get('name')}", message))
    passed = tests - failures - errors - skipped
    totals = f"{passed} passed, {failures} failed, {errors} errors, {skipped} skipped ({seconds:.0f} s)"
    return totals, failed


def _eval_arg(value: str) -> tuple[str, str, Path]:
    name, outcome, log = value.split("=", 2)
    return name, outcome, Path(log)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write_summary(path: Path | None, text: str) -> None:
    if path is None:
        sys.stdout.write(text)
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _default_summary() -> Path | None:
    value = os.environ.get("GITHUB_STEP_SUMMARY")
    return Path(value) if value else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("gate", help="Summary of the offline gate.")
    gate.add_argument("--junit", type=Path, required=True)
    gate.add_argument("--eval", type=_eval_arg, action="append", default=[], metavar="NAME=OUTCOME=LOG")
    gate.add_argument("--summary", type=Path, default=None, help="Default: $GITHUB_STEP_SUMMARY, else stdout.")
    live = commands.add_parser("live", help="Verdict and reports of a live regression run.")
    live.add_argument("--mode", required=True, choices=sorted(MODES))
    live.add_argument("--out", type=Path, required=True, help="The directory holding compare.json.")
    live.add_argument("--status-sha", default="")
    live.add_argument("--pr", default="")
    live.add_argument("--suite", default="")
    live.add_argument("--commit", default="", help="The measured commit.")
    live.add_argument("--summary", type=Path, default=None, help="Default: $GITHUB_STEP_SUMMARY, else stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = args.summary or _default_summary()
    if args.command == "gate":
        _write_summary(summary, render_gate(args.junit, args.eval, os.environ.get("RUNNER_OS", "")))
        return 0
    if args.mode == "before-merge" and not (args.pr and args.status_sha):
        print("error: before-merge needs --pr and --status-sha", file=sys.stderr)
        return 2
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not repository and (args.mode in MAIN_MODES or args.pr):
        _warn("GITHUB_REPOSITORY is not set: GitHub reports are skipped")
    github: GitHub = GhGitHub(repository) if repository else _OfflineGitHub()
    return run_live(
        mode=args.mode,
        out=args.out,
        status_sha=args.status_sha,
        pr=args.pr,
        suite=args.suite,
        commit=args.commit,
        github=github,
        summary_path=summary,
        default=normalize_default(os.environ.get("REGRESSION_DEFAULT")),
    )


class _OfflineGitHub:
    """A local run without GitHub: every report call fails softly."""

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise GitHubError("no GITHUB_REPOSITORY (a local run)")

        return unavailable


if __name__ == "__main__":
    raise SystemExit(main())
