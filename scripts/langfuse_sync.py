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
from scripts.langfuse_bridge.ledger import (
    EXPORTED,
    LEDGER_FILE_NAME,
    LedgerLocked,
    SyncLedger,
)
from scripts.langfuse_bridge.scores import (
    PLATFORM_SCORES_FILE_NAME,
    ensure_score_configs,
    pull_scores,
)
from scripts.langfuse_bridge.sync import SyncReport, SyncUsageError, push_batch
from src.agent_loop.platform_records import review_definitions, score_definitions
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

    experiment = commands.add_parser("experiment", help="Sync batches as experiments.")
    experiment_commands = experiment.add_subparsers(dest="action", required=True)
    push = experiment_commands.add_parser("push", help="Sync one batch (--batch) or every unfinished one (--pending).")
    push.add_argument("--batch", default=None, help="A batch id or directory.")
    push.add_argument("--pending", action="store_true", help="Every batch whose ledger is not complete.")
    push.add_argument("--suite", choices=sorted(SUITES), default="golden")
    push.add_argument("--new-attempt", action="store_true", help="Sync again as a new experiment (new project, reset).")
    push.add_argument("--retry-rejected", action="store_true", help="Send the scores the platform rejected again.")
    push.add_argument("--dry-run", action="store_true", help="Print the plan; read only the files.")

    scores = commands.add_parser("scores", help="Pull the judge's and reviewers' scores of a synced batch.")
    scores_commands = scores.add_subparsers(dest="action", required=True)
    pull = scores_commands.add_parser("pull", help="Write them to <batch>/platform_scores.jsonl.")
    pull.add_argument("--batch", required=True, help="A batch id or directory.")

    commands.add_parser("doctor", help="Check the server version, keys, project and score configs (network).")
    commands.add_parser("setup", help="Create the score configs (archiving outdated ones) and the triage queue.")

    status = commands.add_parser("status", help="Show the sync state of the batches.")
    status.add_argument("--batch", default=None, help="Only this batch (id or directory).")
    status.add_argument("--verify", action="store_true", help="Check exported traces for duplicated observations.")
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
        if args.command == "experiment":
            return _experiment(args, settings, factory, git or run_git(ROOT))
        if args.command == "status":
            return _status(args, settings, factory)
        if args.command == "scores":
            return _pull(args, settings, factory)
        if args.command == "setup":
            return _setup(settings, factory)
        if args.command == "doctor":
            return _doctor(settings, factory)
    except (UsageError, DatasetError, SyncUsageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except LedgerLocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
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


def _experiment(args: argparse.Namespace, settings: Settings, factory: ClientFactory, git: Git) -> int:
    if bool(args.batch) == bool(args.pending):
        raise UsageError("name one batch (--batch) or pass --pending")
    batches_dir = settings.storage.batches_dir
    if args.pending:
        targets = [ledger.parent for ledger in sorted(batches_dir.glob(f"*/{LEDGER_FILE_NAME}"))]
        targets = [path for path in targets if SyncLedger.load(path).data.get("state") != "complete"]
    else:
        targets = [_batch_dir(batches_dir, args.batch)]
    code = 0
    for batch_dir in targets:
        report: SyncReport = push_batch(
            batch_dir,
            settings,
            client_factory=factory,
            repo_tasks=ROOT / SUITES[args.suite],
            git=git,
            suite=args.suite,
            new_attempt=args.new_attempt,
            retry_rejected=args.retry_rejected,
            dry_run=args.dry_run,
        )
        print(report.summary(batch_dir.name))
        code = max(code, report.exit_code())
    return code


def _status(args: argparse.Namespace, settings: Settings, factory: ClientFactory) -> int:
    batches_dir = settings.storage.batches_dir
    if args.batch:
        targets = [_batch_dir(batches_dir, args.batch)]
    else:
        targets = [ledger.parent for ledger in sorted(batches_dir.glob(f"*/{LEDGER_FILE_NAME}"))]
    client = connect(settings, factory) if args.verify else None
    code = 0
    try:
        for batch_dir in targets:
            ledger = SyncLedger.load(batch_dir)
            items = ledger.items
            exported = [name for name, item in items.items() if item.get("state") == EXPORTED]
            line = f"{batch_dir.name} {ledger.data.get('state')} · {len(exported)}/{len(items)} exported"
            url = (ledger.data.get("experiment") or {}).get("url")
            print(line + (f" · {url}" if url else ""))
            if client is not None:
                duplicated = [
                    name
                    for name in exported
                    if len(client.observation_ids(items[name]["trace_id"])) > int(items[name].get("spans") or 0)
                ]
                if duplicated:
                    print(f"  duplicated: {', '.join(duplicated)}")
                    code = 1
    finally:
        if client is not None:
            client.close()
    return code


def _setup(settings: Settings, factory: ClientFactory) -> int:
    queue = settings.eval_platform.triage_queue
    with connect(settings, factory) as client:
        ids = ensure_score_configs(client, score_definitions())
        print(f"score configs: {len(ids)}")
        if queue:
            review = ensure_score_configs(client, review_definitions())
            client.ensure_queue(queue, list(review.values()))
            print(f"annotation queue: {queue}")
    return 0


def _pull(args: argparse.Namespace, settings: Settings, factory: ClientFactory) -> int:
    batch_dir = _batch_dir(settings.storage.batches_dir, args.batch)
    if not (batch_dir / LEDGER_FILE_NAME).is_file():
        raise UsageError(f"{batch_dir.name} was never synced (no {LEDGER_FILE_NAME})")
    with connect(settings, factory) as client:
        rows = pull_scores(client, batch_dir)
    print(f"{len(rows)} scores -> {batch_dir / PLATFORM_SCORES_FILE_NAME}")
    return 0


def _doctor(settings: Settings, factory: ClientFactory) -> int:
    with connect(settings, factory) as client:
        health = client.health()
        project = client.project()
        wanted = {definition.name for definition in score_definitions()}
        present = {config["name"] for config in client.score_configs() if not config.get("isArchived")}
        print(f"ok  server   {client.host} · version {health.version}")
        print(f"ok  keys     project {project.id} ({project.name})")
        mark = "ok" if wanted <= present else "!!"
        hint = "" if wanted <= present else " · run setup"
        print(f"{mark}  configs  score configs {len(wanted & present)} of {len(wanted)}{hint}")
    return 0


def _batch_dir(batches_dir: Path, name: str) -> Path:
    path = Path(name)
    return path if path.is_dir() else batches_dir / name


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
