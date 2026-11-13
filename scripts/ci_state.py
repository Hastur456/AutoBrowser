#!/usr/bin/env python3
"""The CI state of the live regression runs: restore, base selection, save, release verdict.

The state is the ``ci-state`` artifact chain that only main measurements (after merge,
manual) write: the last ``pipeline.ci_state_keep_batches`` batches, the batch history, the
platform dataset journal and ``ci/verdicts.jsonl``, all relative to ``storage.root_dir``.
See docs/development/2026-11-10-regression-ci-cd-design.md §5.6.

Commands:

- ``restore [--archive FILE]`` — unpack the newest unexpired ``ci-state`` artifact of the
  repository (``gh``, ``GITHUB_REPOSITORY``) or a local archive into the home; none is a
  clean start (exit 0).
- ``check-ref --mode MODE`` — a main measurement (``after-merge``, ``manual``) must run a
  commit of ``--branch`` (default ``origin/main``); exit 1 otherwise.
- ``compare --batch-json FILE --out DIR`` — compare the batch ``run_batch.py`` printed with
  its nearest measured ancestor (a batch of the same Golden Set whose commit is an ancestor
  of the new one and the newest such in the commit graph), write ``DIR/compare.json``; the
  exit code is ``compare_batches.py``'s with the perf gate: 0, 1 regression, 2 no base or no
  batch, 3 only slower/costlier.
- ``save --out FILE`` — pack the state for the next run.
- ``verdict --commit SHA`` — exit 0 when ``ci/verdicts.jsonl`` holds a green measurement of
  the commit or an ancestor within ``pipeline.ci_release_verdict_max_age_days``.
- ``tasks --suite NAME`` — print the tasks file of a suite.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.compare_batches import (
    BATCH_FILE_NAME,
    COMPARE_FILE_NAME,
    Batch,
    load_batch,
)
from scripts.langfuse_bridge.datasets import JOURNAL_FILE_NAME, SUITES
from src.agent_loop.batch_compare import compare_batches
from src.agent_loop.batch_history import HISTORY_FILE_NAME
from src.agent_loop.events import read_jsonl_lines
from src.agent_loop.failures import INFRA_KINDS
from src.config import get_settings

#: Name of the artifact holding the state, and of the archive inside it.
STATE_ARTIFACT = "ci-state"
STATE_ARCHIVE_NAME = "ci-state.tar.gz"
#: Directory under ``storage.root_dir`` holding the CI journals.
CI_DIR_NAME = "ci"
VERDICTS_FILE_NAME = "verdicts.jsonl"
#: The report ``compare`` writes into ``--out`` for ``ci_summary.py``.
COMPARE_JSON_NAME = "compare.json"
#: Scratch directory of a batch that never goes into the state.
PROFILES_DIR_NAME = "profiles"
#: Verdicts of a measurement that let a release through.
GREEN_VERDICTS = frozenset({"pass", "no_base", "perf"})
MAIN_MODES = frozenset({"after-merge", "manual"})
MODES = MAIN_MODES | {"before-merge"}

#: ``gh`` runner: arguments in, standard output out (raises on failure).
Gh = Callable[[list[str]], str]
#: ``is_ancestor(ancestor, commit)``: the first commit is the second or one of its ancestors.
Ancestry = Callable[[str, str], bool]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    restore_cmd = commands.add_parser("restore", help="Unpack the latest ci-state into the home.")
    restore_cmd.add_argument("--archive", type=Path, help="A local archive instead of the artifact.")

    check = commands.add_parser("check-ref", help="A main measurement must run a commit of main.")
    check.add_argument("--mode", required=True, choices=sorted(MODES))
    check.add_argument("--branch", default="origin/main", help="Default: origin/main.")
    check.add_argument("--repo", type=Path, default=ROOT, help=argparse.SUPPRESS)

    compare = commands.add_parser("compare", help="Compare a batch with its nearest measured ancestor.")
    compare.add_argument("--batch-json", type=Path, required=True, help="The JSON run_batch.py printed.")
    compare.add_argument("--out", type=Path, required=True, help="Where compare.json is written.")
    compare.add_argument("--repo", type=Path, default=ROOT, help=argparse.SUPPRESS)

    save = commands.add_parser("save", help="Pack the state for the next run.")
    save.add_argument("--out", type=Path, required=True)

    verdict = commands.add_parser("verdict", help="Is there a recent green measurement of the commit?")
    verdict.add_argument("--commit", required=True)
    verdict.add_argument("--repo", type=Path, default=ROOT, help=argparse.SUPPRESS)

    tasks = commands.add_parser("tasks", help="Print the tasks file of a suite.")
    tasks.add_argument("--suite", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    root = settings.storage.root_dir
    if args.command == "restore":
        if args.archive is not None:
            unpack_state(args.archive, root)
            print(f"restored {args.archive} into {root}")
            return 0
        restore(root, run_gh)
        return 0
    if args.command == "check-ref":
        return check_ref(args.mode, args.branch, git_ancestry(args.repo), _head(args.repo))
    if args.command == "compare":
        report = compare_with_base(args.batch_json, settings.storage.batches_dir, root, git_ancestry(args.repo))
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / COMPARE_JSON_NAME).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _print_compare(report)
        return int(report["exit_code"])
    if args.command == "save":
        kept = pack_state(root, settings.storage.batches_dir, settings.pipeline.ci_state_keep_batches, args.out)
        print(f"saved {len(kept)} batches into {args.out}: {', '.join(kept) or '-'}")
        return 0
    if args.command == "verdict":
        rows = read_verdicts(root)
        found = green_verdict(
            rows,
            args.commit,
            git_ancestry(args.repo),
            now=datetime.now(UTC),
            max_age_days=settings.pipeline.ci_release_verdict_max_age_days,
        )
        if found is None:
            print(
                f"error: no green main measurement of {args.commit} or an ancestor in the last "
                f"{settings.pipeline.ci_release_verdict_max_age_days} days "
                f"(latest: {_latest_summary(rows)}); run the regression workflow by hand with "
                "ref=<the tag>, or set RELEASE_REQUIRES_REGRESSION=false",
                file=sys.stderr,
            )
            return 1
        print(f"green: {found.get('verdict')} on {found.get('commit')} at {found.get('finished_at')}")
        return 0
    if args.command == "tasks":
        if args.suite not in SUITES:
            print(f"error: unknown suite {args.suite!r}; known: {', '.join(SUITES)}", file=sys.stderr)
            return 2
        print(SUITES[args.suite])
        return 0
    raise AssertionError(args.command)


# --------------------------------------------------------------------------
# git and gh
# --------------------------------------------------------------------------


def git_ancestry(repo: Path) -> Ancestry:
    """``git merge-base --is-ancestor`` in ``repo``; an unknown commit is no ancestor."""

    def is_ancestor(ancestor: str, commit: str) -> bool:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, commit],
            cwd=repo,
            capture_output=True,
            check=False,
        )
        return completed.returncode == 0

    return is_ancestor


def _head(repo: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, encoding="utf-8", check=True
    )
    return completed.stdout.strip()


def run_gh(argv: list[str]) -> str:
    """Run the GitHub CLI (``GH_TOKEN`` from the environment); output decoded as UTF-8."""

    completed = subprocess.run(
        ["gh", *argv], capture_output=True, encoding="utf-8", errors="replace", check=True
    )
    return completed.stdout


def check_ref(mode: str, branch: str, is_ancestor: Ancestry, head: str) -> int:
    if mode not in MAIN_MODES:
        return 0
    if is_ancestor(head, branch):
        print(f"{head} is on {branch}")
        return 0
    print(f"error: a {mode} measurement must run a commit of {branch}; {head} is not on it", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------
# restore / save
# --------------------------------------------------------------------------


def restore(root: Path, gh: Gh) -> bool:
    """Unpack the newest unexpired ``ci-state`` artifact into ``root``; ``False`` without one."""

    repository = _environ("GITHUB_REPOSITORY")
    listing = json.loads(gh(["api", f"repos/{repository}/actions/artifacts?name={STATE_ARTIFACT}&per_page=100"]))
    artifacts = [a for a in listing.get("artifacts") or [] if not a.get("expired")]
    if not artifacts:
        print("no ci-state artifact yet: a clean start, the next main measurement becomes the base")
        return False
    newest = max(artifacts, key=lambda artifact: str(artifact.get("created_at") or ""))
    run_id = str(newest["workflow_run"]["id"])
    with tempfile.TemporaryDirectory() as scratch:
        gh(["run", "download", run_id, "-n", STATE_ARTIFACT, "-D", scratch])
        unpack_state(Path(scratch) / STATE_ARCHIVE_NAME, root)
    print(f"restored ci-state of run {run_id} into {root}")
    return True


def unpack_state(archive: Path, root: Path) -> None:
    """Extract ``archive`` into ``root``; a member escaping ``root`` is a ``TarError``."""

    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(root, filter="data")


def pack_state(root: Path, batches_dir: Path, keep: int, out: Path) -> list[str]:
    """Pack the newest ``keep`` finished batches of every Golden Set (without their profiles)
    and the journals, so runs of one suite never evict the base of another.

    Returns the ids of the kept batches.
    """

    by_tasks: dict[str, list[Batch]] = {}
    for batch in _finished_batches(batches_dir):
        by_tasks.setdefault(_tasks_of(batch[1]), []).append(batch)
    kept = sorted(
        (
            batch
            for runs in by_tasks.values()
            for batch in sorted(runs, key=lambda b: str(b[1].get("started_at") or ""))[-keep:]
        ),
        key=lambda b: str(b[1].get("started_at") or ""),
    )
    journals = [
        batches_dir / HISTORY_FILE_NAME,
        batches_dir / JOURNAL_FILE_NAME,
        root / CI_DIR_NAME / VERDICTS_FILE_NAME,
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        for batch_dir, _meta, _rows in kept:
            for path in sorted(batch_dir.rglob("*")):
                relative = path.relative_to(batch_dir)
                if path.is_file() and relative.parts[0] != PROFILES_DIR_NAME:
                    tar.add(path, arcname=path.relative_to(root).as_posix())
        for journal in journals:
            if journal.is_file():
                tar.add(journal, arcname=journal.relative_to(root).as_posix())
    return [str(meta.get("batch_id") or batch_dir.name) for batch_dir, meta, _rows in kept]


def _finished_batches(batches_dir: Path) -> list[Batch]:
    batches: list[Batch] = []
    for batch_file in sorted(Path(batches_dir).glob(f"*/{BATCH_FILE_NAME}")):
        try:
            batch = load_batch(batch_file.parent)
        except (OSError, ValueError) as exc:
            print(f"warning: skipping {batch_file.parent.name}: {exc}", file=sys.stderr)
            continue
        if batch[1].get("finished_at"):
            batches.append(batch)
    return batches


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------


def compare_with_base(batch_json: Path, batches_dir: Path, root: Path, is_ancestor: Ancestry) -> dict[str, Any]:
    """The ``compare.json`` report of a new batch against its nearest measured ancestor."""

    try:
        result = json.loads(Path(batch_json).read_text(encoding="utf-8"))
        new = load_batch(Path(str(result["batch_dir"])))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {
            "status": "no_batch",
            "exit_code": 2,
            "error": f"{type(exc).__name__}: {exc}",
            "batch": None,
            "base": None,
            "comparison": None,
        }
    batch_dir, meta, rows = new
    commit = _commit_of(meta)
    verdicts = read_verdicts(root)
    max_ratio = get_settings().pipeline.ci_max_inconclusive_ratio
    candidates = [
        batch
        for batch in _finished_batches(batches_dir)
        if batch[0].resolve() != batch_dir.resolve()
        and _tasks_of(batch[1]) == _tasks_of(meta)
        and _commit_of(batch[1])
        and commit
        and _trusted_base(batch, verdicts, max_ratio)
        and is_ancestor(_commit_of(batch[1]), commit)
    ]
    base = nearest(candidates, is_ancestor)
    report: dict[str, Any] = {
        "batch": _batch_summary(new),
        "base": None,
        "comparison": None,
    }
    if base is None:
        return {"status": "no_base", "exit_code": 2, **report}
    comparison = compare_batches(base[1], base[2], meta, rows, get_settings().pipeline)
    (batch_dir / COMPARE_FILE_NAME).write_text(comparison.render_markdown(), encoding="utf-8")
    pr = _verdict_pr(verdicts, str(base[1].get("batch_id") or base[0].name))
    return {
        **report,
        "status": "compared" if comparison.comparable else "no_base",
        "exit_code": comparison.exit_code(perf_gate=True),
        "base": {**_batch_summary(base), "pr": pr},
        "comparison": comparison.to_json_dict(perf_gate=True),
    }


def _trusted_base(batch: Batch, verdicts: Sequence[Mapping[str, Any]], max_ratio: float) -> bool:
    """A batch may be a base unless its measurement was infra: journalled so, or with more infra
    failures than the allowed share. An infra kind in the base turns a new agent failure into
    ``changed`` instead of ``regression``, so such a base would hide regressions."""

    batch_id = str(batch[1].get("batch_id") or batch[0].name)
    journalled = [row.get("verdict") for row in verdicts if row.get("batch_id") == batch_id]
    if journalled and journalled[-1] == "infra":
        return False
    counts = _batch_summary(batch)["counts"]
    return not counts["total"] or counts["infra"] / counts["total"] <= max_ratio


def nearest(candidates: Sequence[Batch], is_ancestor: Ancestry) -> Batch | None:
    """The candidate no other candidate descends from; the latest started among ties.

    All candidates are ancestors of one commit, so on a linear main exactly one commit is
    the newest; a re-measured commit has several batches, and the latest run of it wins.
    """

    tips = [
        batch
        for batch in candidates
        if not any(
            _commit_of(other[1]) != _commit_of(batch[1]) and is_ancestor(_commit_of(batch[1]), _commit_of(other[1]))
            for other in candidates
        )
    ]
    return max(tips, key=lambda batch: str(batch[1].get("started_at") or ""), default=None)


def _batch_summary(batch: Batch) -> dict[str, Any]:
    batch_dir, meta, rows = batch
    kinds = [str(row.get("failure_kind") or "") for row in rows]
    infra = sum(1 for kind in kinds if kind in INFRA_KINDS)
    passed = kinds.count("passed")
    ungraded = kinds.count("ungraded")
    return {
        "batch_id": str(meta.get("batch_id") or batch_dir.name),
        "dir": str(batch_dir),
        "commit": _commit_of(meta),
        "started_at": meta.get("started_at"),
        "counts": {
            "total": len(rows),
            "passed": passed,
            "agent": len(rows) - passed - infra - ungraded,
            "infra": infra,
            "ungraded": ungraded,
        },
    }


def _commit_of(meta: Mapping[str, Any]) -> str:
    return str(((meta.get("provenance") or {}).get("git") or {}).get("commit") or "")


def _tasks_of(meta: Mapping[str, Any]) -> str:
    return str((meta.get("provenance") or {}).get("tasks_sha256") or "")


def _print_compare(report: Mapping[str, Any]) -> None:
    base = report.get("base") or {}
    print(f"compare: {report['status']} (exit {report['exit_code']}), base {base.get('batch_id') or '-'}")
    if report.get("error"):
        print(f"error: {report['error']}", file=sys.stderr)
    for finding in (report.get("comparison") or {}).get("findings") or []:
        print(f"{finding['kind']}: {finding['scenario_id']} {finding.get('before')} -> {finding.get('after')}")


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------


def verdicts_path(root: Path) -> Path:
    return root / CI_DIR_NAME / VERDICTS_FILE_NAME


def append_verdict(root: Path, row: Mapping[str, Any]) -> None:
    """Append one measurement's verdict to ``ci/verdicts.jsonl`` (only main measurements)."""

    path = verdicts_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def read_verdicts(root: Path) -> list[dict[str, Any]]:
    path = verdicts_path(root)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in read_jsonl_lines(path):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def green_verdict(
    rows: Iterable[Mapping[str, Any]],
    commit: str,
    is_ancestor: Ancestry,
    *,
    now: datetime,
    max_age_days: int,
) -> Mapping[str, Any] | None:
    """The newest green measurement of ``commit`` or an ancestor not older than the window."""

    oldest = now - timedelta(days=max_age_days)
    green = [
        row
        for row in rows
        if row.get("verdict") in GREEN_VERDICTS
        and row.get("commit")
        and (finished := _parse_time(row.get("finished_at"))) is not None
        and finished >= oldest
        and is_ancestor(str(row["commit"]), commit)
    ]
    return max(green, key=lambda row: str(row.get("finished_at")), default=None)


def _verdict_pr(rows: Iterable[Mapping[str, Any]], batch_id: str) -> str | None:
    pr = next((row.get("pr") for row in reversed(list(rows)) if row.get("batch_id") == batch_id), None)
    return str(pr) if pr else None


def _latest_summary(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "none"
    row = rows[-1]
    return f"{row.get('verdict')} on {row.get('commit')} at {row.get('finished_at')}"


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _environ(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"error: {name} is not set (it is set on GitHub Actions)")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
