#!/usr/bin/env python3
"""Decide which live regression mode a pull request event asks for (standard library only).

Reads the ``pull_request`` event (``--event``, default ``$GITHUB_EVENT_PATH``) and the
repository default ``REGRESSION_DEFAULT`` (``off`` | ``before`` | ``after`` | ``both``) and
prints GitHub step outputs:

- ``before=run|pass|keep`` — run before merge (status ``pending``), write ``success`` without a
  run, or write nothing, so an unrelated event never masks the last result;
- ``after=run|none`` — run on the merge commit;
- ``status_sha`` — the PR head commit the ``regression/before-merge`` status belongs to;
- ``reason`` — the status description.

A label event compares "requested" before and after it, so only a label that flips it counts.
See docs/development/2026-11-10-regression-ci-cd-design.md §5.3.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BEFORE_LABEL = "regression:before-merge"
AFTER_LABEL = "regression:after-merge"
SKIP_LABEL = "regression:skip"
DEFAULT_ENV = "REGRESSION_DEFAULT"
DEFAULTS = ("off", "before", "after", "both")
NEW_HEAD_ACTIONS = frozenset({"opened", "reopened", "synchronize"})
LABEL_ACTIONS = frozenset({"labeled", "unlabeled"})

REASON_NOT_REQUESTED = "Live regression not requested"
REASON_REQUESTED = "Live regression run in progress"
REASON_REMOVED = "Live regression run removed (label or default)"
REASON_UNCHANGED = "Event does not change the requested mode"
REASON_FORK = "Pull request from a fork: no status, no live run"
REASON_NO_HEAD = "Event does not change the head commit"


@dataclass(frozen=True)
class Decision:
    before: str
    after: str
    status_sha: str
    reason: str

    def outputs(self) -> str:
        reason = " ".join(self.reason.split())
        return f"before={self.before}\nafter={self.after}\nstatus_sha={self.status_sha}\nreason={reason}\n"


def normalize_default(value: str | None) -> str:
    """``REGRESSION_DEFAULT`` as one of :data:`DEFAULTS`; anything else is ``off`` and a warning."""

    text = (value or "off").strip().lower()
    if text in DEFAULTS:
        return text
    print(
        f"warning: {DEFAULT_ENV}={value!r} is not one of {', '.join(DEFAULTS)}; using off",
        file=sys.stderr,
    )
    return "off"


def requested(labels: set[str], default: str) -> tuple[bool, bool]:
    """Whether the before-merge and the after-merge runs are requested by these labels."""

    skip = SKIP_LABEL in labels
    before = BEFORE_LABEL in labels or (default in {"before", "both"} and not skip)
    after = AFTER_LABEL in labels or (default in {"after", "both"} and not skip)
    return before, after


def decide(event: dict[str, Any], default: str) -> Decision:
    action = str(event.get("action") or "")
    pr = event.get("pull_request") or {}
    head = pr.get("head") or {}
    sha = str(head.get("sha") or "")
    labels = {str(label.get("name")) for label in pr.get("labels") or [] if isinstance(label, dict)}
    head_repo = ((head.get("repo") or {}).get("full_name")) or ""
    base_repo = (((pr.get("base") or {}).get("repo") or {}).get("full_name")) or ""
    if head_repo != base_repo:
        # A fork's token is read-only and gets no secrets (design §3.4).
        return Decision("keep", "none", sha, REASON_FORK)

    before_now, after_now = requested(labels, default)
    if action in NEW_HEAD_ACTIONS:
        if before_now:
            return Decision("run", "none", sha, REASON_REQUESTED)
        return Decision("pass", "none", sha, REASON_NOT_REQUESTED)
    if action in LABEL_ACTIONS:
        changed = str((event.get("label") or {}).get("name") or "")
        prior = labels - {changed} if action == "labeled" else labels | {changed}
        before_prior, _ = requested(prior, default)
        if before_prior == before_now:
            return Decision("keep", "none", sha, REASON_UNCHANGED)
        if before_now:
            return Decision("run", "none", sha, REASON_REQUESTED)
        return Decision("pass", "none", sha, REASON_REMOVED)
    if action == "closed" and pr.get("merged") and after_now:
        return Decision("keep", "run", sha, "Live regression after merge")
    return Decision("keep", "none", sha, REASON_NO_HEAD)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"), help="Event JSON.")
    args = parser.parse_args(argv)
    if args.event is None:
        print("error: no --event and no GITHUB_EVENT_PATH", file=sys.stderr)
        return 2
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    decision = decide(event, normalize_default(os.environ.get(DEFAULT_ENV)))
    sys.stdout.write(decision.outputs())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
