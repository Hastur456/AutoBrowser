#!/usr/bin/env python3
"""Sync local batch results to Langfuse: datasets, experiments, scores (eval_platform).

The batch folders stay the source of truth and the only source of the regression verdict;
Langfuse is a second view built from them. See docs/development/eval-platform.md.

Exit codes: 0 everything synced or nothing to do; 1 partly synced (an outage, items still
pending or unknown, rejected scores) - rerun later; 2 a usage or configuration problem (the
platform is disabled, keys are missing or wrong, the server is too old, a dirty tasks file).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.langfuse_bridge.client import LangfuseClient, LangfuseError
from scripts.langfuse_bridge.datasets import (
    JOURNAL_FILE_NAME,
    DatasetError,
    DatasetPlan,
    Git,
    dataset_status,
    push_dataset,
    run_git,
)
from src.config import EvalPlatformSettings, Settings, get_settings

#: Scenario sets that can be pushed as datasets, and their files in the repository.
SUITES = {"golden": "tests/golden/tasks.jsonl"}

ClientFactory = Callable[[EvalPlatformSettings], LangfuseClient]


class UsageError(Exception):
    """A usage or configuration problem: exit 2."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    dataset = commands.add_parser("dataset", help="Push a scenario set as a dataset, or show what would change.")
    dataset_commands = dataset.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("push", "Upsert the changed scenarios, archive the removed ones."),
        ("status", "Show added/changed/archived/unchanged scenarios and edits made on the platform."),
    ):
        sub = dataset_commands.add_parser(action, help=help_text)
        sub.add_argument("--suite", choices=sorted(SUITES), default="golden")
        sub.add_argument("--tasks", type=Path, default=None, help="Tasks file. Default: the suite's file.")
        if action == "push":
            sub.add_argument("--allow-dirty", action="store_true", help="Push a tasks file that differs from HEAD.")
            sub.add_argument("--dry-run", action="store_true", help="Print the plan; read only the file.")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
    git: Git | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    factory = client_factory or LangfuseClient
    try:
        if args.command == "dataset":
            return _dataset(args, settings, factory, git or run_git(ROOT))
    except (UsageError, DatasetError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except LangfuseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2 if exc.status in {401, 403} else 1
    raise AssertionError(args.command)  # pragma: no cover - argparse requires a command


def connect(settings: Settings, factory: ClientFactory) -> LangfuseClient:
    """A client for an enabled, configured platform whose server is new enough."""

    platform = settings.eval_platform
    if not platform.enabled:
        raise UsageError("eval_platform.enabled is false")
    try:
        client = factory(platform)
    except LangfuseError as exc:
        raise UsageError(str(exc)) from None
    health = client.health()
    if not health.compatible:
        raise UsageError(f"the server reports version {health.version!r}; this bridge needs Langfuse >= 4.59.0")
    return client


def _dataset(args: argparse.Namespace, settings: Settings, factory: ClientFactory, git: Git) -> int:
    tasks = Path(args.tasks) if args.tasks else ROOT / SUITES[args.suite]
    source_path = _repo_relative(tasks)
    platform = settings.eval_platform
    if args.action == "push" and args.dry_run:
        result = push_dataset(
            None,
            platform,
            suite=args.suite,
            tasks_path=tasks,
            source_path=source_path,
            journal_path=settings.storage.batches_dir / JOURNAL_FILE_NAME,
            git=git,
            dry_run=True,
        )
        print(f"dataset {result.dataset} (dry run): {len(result.added)} scenarios to push from {source_path}")
        return 0
    with connect(settings, factory) as client:
        if args.action == "push":
            result = push_dataset(
                client,
                platform,
                suite=args.suite,
                tasks_path=tasks,
                source_path=source_path,
                journal_path=settings.storage.batches_dir / JOURNAL_FILE_NAME,
                git=git,
                allow_dirty=args.allow_dirty,
            )
            print(
                f"dataset {result.dataset}: {_counts(result)} · version {result.version or 'unknown'}"
                f"{' · git dirty' if result.git_dirty else ''}"
            )
            _print_edited(result)
            return 0
        status = dataset_status(client, platform, suite=args.suite, tasks_path=tasks, source_path=source_path)
        print(f"dataset {platform.dataset_prefix}{args.suite}: {_counts(status)}")
        for label, names in (("added", status.added), ("changed", status.updated), ("archived", status.archived)):
            if names:
                print(f"{label}: {', '.join(names)}")
        _print_edited(status)
        return 0


def _counts(plan: DatasetPlan) -> str:
    return (
        f"{len(plan.added)} added · {len(plan.updated)} updated · {len(plan.archived)} archived"
        f" · {len(plan.unchanged)} unchanged"
    )


def _print_edited(plan: DatasetPlan) -> None:
    if plan.edited_remotely:
        print(f"edited on the platform (overwritten by the next change): {', '.join(plan.edited_remotely)}")


def _repo_relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
