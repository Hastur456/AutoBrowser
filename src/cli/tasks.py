"""Task argument resolution helpers for CLI entry points."""

from __future__ import annotations

import argparse


def resolve_task(args: argparse.Namespace) -> str:
    """Resolve a task from positional args, --task, or stdin prompt."""

    task = " ".join(args.task).strip()
    if task:
        return task
    if args.task_text:
        return args.task_text.strip()
    return input("Задача> ").strip()


def resolve_initial_task(args: argparse.Namespace) -> str | None:
    """Resolve the optional startup task without prompting."""

    task = " ".join(args.task).strip()
    if task:
        return task
    if args.task_text:
        return args.task_text.strip()
    return None


__all__ = ["resolve_initial_task", "resolve_task"]
