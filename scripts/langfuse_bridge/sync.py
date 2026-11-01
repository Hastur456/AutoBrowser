"""``push_batch``: sync one finished batch folder to Langfuse, idempotently (§5.5, §5.6).

Order: lock -> connect (health, project) -> dataset (push it when the batch ran on the
repository's current scenario set; map the batch's set to a Langfuse version through the
journal; create missing items as archived) -> item traces, one scenario at a time, each
recorded in the ledger before and after it is sent. A trace marked ``exported`` is never
sent again (Langfuse v4 does not deduplicate spans); one whose request may have arrived is
``unknown`` and is verified through the observations API before it is ever re-sent. A sync
that runs out of budget, or meets an outage, stops between items and leaves the rest
``pending`` for ``langfuse_sync.py experiment push --pending``. Failures never raise into
``run_batch.py`` except usage problems (:class:`SyncUsageError`).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

from scripts.langfuse_bridge.client import LangfuseClient, LangfuseError
from scripts.langfuse_bridge.datasets import (
    ARCHIVED,
    JOURNAL_FILE_NAME,
    DatasetError,
    Git,
    dataset_name,
    item_body,
    item_id,
    journaled_version,
    push_dataset,
)
from scripts.langfuse_bridge.experiments import (
    BATCH_FILE_NAME,
    BatchFolder,
    ExperimentRef,
    experiment_id,
    experiment_name,
    item_trace,
    load_batch_folder,
)
from scripts.langfuse_bridge.ledger import (
    COMPLETE,
    EXPORTED,
    PENDING,
    SENDING,
    UNKNOWN,
    LedgerLocked,
    SyncLedger,
    ledger_lock,
)
from scripts.langfuse_bridge.scores import (
    batch_records,
    ensure_score_configs,
    local_comparison,
    score_bodies,
)
from src.agent_loop.batch import tasks_sha256
from src.agent_loop.batch_compare import previous_comparable
from src.agent_loop.events import redact_json_safe
from src.agent_loop.failures import FailureKind
from src.agent_loop.platform_records import review_definitions, score_definitions
from src.config import Settings

logger = logging.getLogger(__name__)

SyncState = Literal["complete", "partial", "pending", "skipped"]
ClientFactory = Callable[..., LangfuseClient]
_SUPPORTED_SCHEMA = 1


class SyncUsageError(Exception):
    """A sync that must not run: a usage or configuration problem (exit 2)."""


@dataclass(frozen=True)
class SyncReport:
    state: SyncState
    experiment_url: str | None = None
    exported: int = 0
    unknown: int = 0
    pending: int = 0
    scores_accepted: int = 0
    scores_rejected: int = 0
    enqueued: int = 0
    error: str | None = None

    def exit_code(self) -> int:
        return 0 if self.state in {"complete", "skipped"} else 1

    def summary(self, batch_id: str) -> str:
        line = (
            f"langfuse: {batch_id} {self.state} · {self.exported} exported · {self.unknown} unknown"
            f" · {self.pending} pending · {self.scores_accepted} scores"
        )
        if self.scores_rejected:
            line += f" ({self.scores_rejected} rejected)"
        if self.enqueued:
            line += f" · {self.enqueued} queued"
        if self.experiment_url:
            line += f" · {self.experiment_url}"
        if self.error:
            line += f" · {self.error}"
        return line


def push_batch(
    batch_dir: Path,
    settings: Settings,
    *,
    client_factory: ClientFactory = LangfuseClient,
    repo_tasks: Path | None,
    git: Git,
    suite: str = "golden",
    deadline: float | None = None,
    new_attempt: bool = False,
    retry_rejected: bool = False,
    dry_run: bool = False,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SyncReport:
    """Sync ``batch_dir``; ``deadline`` is a ``clock()`` value after which no new item starts."""

    platform = settings.eval_platform
    folder = _load(batch_dir, dataset_name(platform, suite), suite)
    if dry_run:
        return SyncReport(state="skipped", pending=len(folder.rows))
    if not platform.enabled:
        raise SyncUsageError("eval_platform.enabled is false")

    with ledger_lock(batch_dir, stale_after_seconds=2 * platform.sync_timeout_seconds, now=now):
        ledger = SyncLedger.load(batch_dir)
        if ledger.data.get("state") == COMPLETE and not new_attempt and not retry_rejected:
            return _report_from(ledger, "complete")
        run = _Run(folder, settings, ledger, suite, clock, now, deadline)
        try:
            client = client_factory(platform)
        except LangfuseError as exc:
            raise SyncUsageError(str(exc)) from None
        with client:
            try:
                return run.sync(
                    client, repo_tasks=repo_tasks, git=git, new_attempt=new_attempt, retry_rejected=retry_rejected
                )
            except LangfuseError as exc:
                run.fail(exc)
                if exc.status in {401, 403}:
                    raise SyncUsageError(str(exc)) from None
                return run.report()


def _load(batch_dir: Path, dataset: str, suite: str) -> BatchFolder:
    if not (Path(batch_dir) / BATCH_FILE_NAME).is_file():
        raise SyncUsageError(f"{batch_dir} is not a batch folder (no {BATCH_FILE_NAME})")
    folder = load_batch_folder(batch_dir, dataset=dataset, suite=suite)
    if folder.metadata.get("schema_version") != _SUPPORTED_SCHEMA:
        raise SyncUsageError(
            f"{folder.path.name} has no schema_version {_SUPPORTED_SCHEMA}: a batch from before the"
            " analytics contract (no failure kinds, traces or provenance) cannot be synced"
        )
    if not folder.metadata.get("finished_at"):
        raise SyncUsageError(f"{folder.path.name} has not finished")
    return folder


class _Run:
    """One sync pass over one batch, with its ledger kept up to date step by step."""

    def __init__(
        self,
        folder: BatchFolder,
        settings: Settings,
        ledger: SyncLedger,
        suite: str,
        clock: Callable[[], float],
        now: Callable[[], datetime],
        deadline: float | None,
    ) -> None:
        self.folder = folder
        self.settings = settings
        self.platform = settings.eval_platform
        self.ledger = ledger
        self.suite = suite
        self.clock = clock
        self.now = now
        self.deadline = deadline

    # -- the pass ---------------------------------------------------------------------------

    def sync(
        self,
        client: LangfuseClient,
        *,
        repo_tasks: Path | None,
        git: Git,
        new_attempt: bool,
        retry_rejected: bool,
    ) -> SyncReport:
        health = client.health()
        if not health.compatible:
            raise SyncUsageError(f"the server reports version {health.version!r}; this bridge needs Langfuse >= 4.59.0")
        project = client.project()
        data = self.ledger.data
        if data.get("project_id") and data["project_id"] != project.id and not new_attempt:
            raise SyncUsageError(
                f"{self.folder.path.name} was synced to project {data['project_id']}, the keys now name"
                f" {project.id}; pass --new-attempt to sync it again as a new experiment"
            )
        if new_attempt and data.get("experiment"):
            attempt = int(data["experiment"].get("attempt") or 1) + 1
            self.ledger.data = {**SyncLedger.empty(), "attempts_before": attempt - 1}
            data = self.ledger.data
        else:
            attempt = int((data.get("experiment") or {}).get("attempt") or 1)
        data.update(
            host=client.host,
            project_id=project.id,
            capture_content=data.get("capture_content") or self.platform.capture_content,
            max_request_bytes=data.get("max_request_bytes") or self.platform.max_request_bytes,
        )

        if not data.get("dataset"):
            data["dataset"] = self._dataset(client, project.id, repo_tasks, git)
        if data.get("baseline_batch_id") is None:
            data["baseline_batch_id"] = self._baseline()
        dataset = data["dataset"]
        batch_id = str(self.folder.metadata["batch_id"])
        ref = ExperimentRef(
            id=experiment_id(batch_id, attempt),
            name=experiment_name(self.folder.metadata, self.suite),
            dataset_name=dataset["name"],
            dataset_id=dataset["id"],
            item_version=dataset.get("version"),
            version_matched=bool(dataset.get("version_matched")),
            baseline_batch_id=data.get("baseline_batch_id") or None,
        )
        data["experiment"] = {
            "id": ref.id,
            "name": ref.name,
            "attempt": attempt,
            "url": f"{client.host}/project/{project.id}/datasets/{dataset['id']}",
        }
        self._save()

        self._export_items(client, ref)
        if self._all_exported():
            self._scores(client, ref, retry_rejected=retry_rejected)
            if self.platform.triage_queue:
                self._triage(client)
        return self.finish()

    def _export_items(self, client: LangfuseClient, ref: ExperimentRef) -> None:
        platform = self.platform.model_copy(
            update={
                "capture_content": self.ledger.data["capture_content"],
                "max_request_bytes": self.ledger.data["max_request_bytes"],
            }
        )
        service = self.settings.telemetry.service_name
        for row in self.folder.rows:
            scenario = str(row.get("scenario_id") or "")
            item = self.ledger.items.setdefault(scenario, {"state": PENDING, "parts_sent": 0})
            if item["state"] == EXPORTED:
                continue
            trace = item_trace(self.folder, row, ref, platform, service_name=service)
            item.update(trace_id=trace.trace_id, root_span_id=trace.root_span_id, parts=len(trace.payloads))
            item["spans"] = sum(
                len(scope["spans"]) for payload in trace.payloads for rs in payload["resourceSpans"] for scope in rs["scopeSpans"]
            )
            if item["state"] in {UNKNOWN, SENDING} and not self._verify(client, item, trace.payloads):
                continue
            if self.deadline is not None and self.clock() > self.deadline:
                return  # out of budget: stop between items, never mid-item
            if not self._send(client, item, trace.payloads):
                return

    def _verify(self, client: LangfuseClient, item: dict[str, Any], payloads: list[dict[str, Any]]) -> bool:
        """Resolve an ``unknown`` part: ``True`` when the item may go on being sent."""

        sent_at = datetime.fromisoformat(str(item.get("sent_at") or self.now().isoformat()))
        if (self.now() - sent_at).total_seconds() < self.platform.verify_grace_seconds:
            return False
        part = payloads[int(item.get("parts_sent") or 0)]
        first_span = part["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"]
        if first_span in set(client.observation_ids(item["trace_id"])):
            item["parts_sent"] = int(item.get("parts_sent") or 0) + 1
        item["state"] = PENDING
        if item["parts_sent"] >= len(payloads):
            item.update(state=EXPORTED, exported_at=self.now().isoformat())
        self._save()
        return item["state"] == PENDING

    def _send(self, client: LangfuseClient, item: dict[str, Any], payloads: list[dict[str, Any]]) -> bool:
        """Send the remaining parts; ``False`` when the platform failed and the pass must stop."""

        for index in range(int(item.get("parts_sent") or 0), len(payloads)):
            item.update(state=SENDING, sent_at=self.now().isoformat())
            self._save()
            try:
                client.export_otlp(payloads[index])
            except LangfuseError as exc:
                if exc.delivery_unknown:
                    item["state"] = UNKNOWN
                    self._save()
                    return True  # the next item is independent of this one
                item["state"] = PENDING
                self.fail(exc)
                if exc.status in {401, 403}:
                    raise
                return False
            item["parts_sent"] = index + 1
            item["requests"] = int(item.get("requests") or 0) + 1
        item.update(state=EXPORTED, exported_at=self.now().isoformat())
        self._save()
        return True

    def _scores(self, client: LangfuseClient, ref: ExperimentRef, *, retry_rejected: bool) -> None:
        """Send the batch's scores once. After a partial rejection, only ``retry_rejected``
        sends again — the whole set, since Langfuse does not name the rejected scores and an
        unchanged score is an upsert (same id and timestamp)."""

        previous = self.ledger.data.get("scores") or {}
        rejected_before = list(previous.get("rejected") or [])
        if previous.get("state") == "posted" and not (retry_rejected and rejected_before):
            return
        comparison = local_comparison(self.folder, self.ledger.data.get("baseline_batch_id"), self.settings.pipeline)
        bodies = score_bodies(
            self.folder,
            batch_records(self.folder, comparison),
            experiment=ref.id,
            items=self.ledger.items,
            config_ids=ensure_score_configs(client, score_definitions()),
            environment=self.platform.environment,
        )
        accepted = 0
        rejected: list[dict[str, Any]] = []
        size = self.platform.score_batch_size
        for start in range(0, len(bodies), size):
            chunk = bodies[start : start + size]
            result = client.post_scores(chunk)
            accepted += result.accepted
            messages = [str(error.get("message") or "") for error in result.errors]
            messages += [""] * max(result.rejected - len(messages), 0)
            rejected += [{"message": redact_json_safe(message)} for message in messages]
        self.ledger.data["scores"] = {
            "state": "posted",
            "timestamp": str(self.folder.metadata.get("finished_at")),
            "accepted": accepted,
            "rejected": rejected,
        }
        self._save()

    def _triage(self, client: LangfuseClient) -> None:
        """Queue the item traces of failed scenarios for review, each once per experiment."""

        known = set(get_args(FailureKind))
        kinds = {kind for kind in self.platform.triage_kinds if kind in known}
        for unknown in sorted(set(self.platform.triage_kinds) - known):
            logger.warning("eval_platform.triage_kinds: %r is not a failure kind; ignored", unknown)
        triage = self.ledger.data.get("triage") or {}
        enqueued = list(triage.get("enqueued") or [])
        candidates = [
            str(row.get("scenario_id"))
            for row in self.folder.rows
            if row.get("failure_kind") in kinds and str(row.get("scenario_id")) not in enqueued
        ]
        room = max(self.platform.triage_max_items - len(enqueued), 0)
        if not candidates[:room]:
            return
        queue_id = triage.get("queue_id")
        if not queue_id:
            config_ids = ensure_score_configs(client, review_definitions())
            queue_id = client.ensure_queue(str(self.platform.triage_queue), list(config_ids.values()))
        for scenario in candidates[:room]:
            client.enqueue(queue_id, self.ledger.items[scenario]["trace_id"])
            enqueued.append(scenario)
            self.ledger.data["triage"] = {"queue_id": queue_id, "enqueued": enqueued}
            self._save()

    def _all_exported(self) -> bool:
        items = self.ledger.items
        return len(self.folder.rows) > 0 and all(
            items.get(str(row.get("scenario_id")), {}).get("state") == EXPORTED for row in self.folder.rows
        )

    # -- dataset and baseline ---------------------------------------------------------------

    def _dataset(self, client: LangfuseClient, project_id: str, repo_tasks: Path | None, git: Git) -> dict[str, Any]:
        name = dataset_name(self.platform, self.suite)
        batch_hash = str((self.folder.metadata.get("provenance") or {}).get("tasks_sha256") or "")
        journal = self.settings.storage.batches_dir / JOURNAL_FILE_NAME
        if repo_tasks is not None and Path(repo_tasks).is_file() and tasks_sha256(repo_tasks) == batch_hash:
            try:
                push_dataset(
                    client,
                    self.platform,
                    suite=self.suite,
                    tasks_path=Path(repo_tasks),
                    source_path=_source_path(self.folder.metadata, repo_tasks),
                    journal_path=journal,
                    git=git,
                )
            except DatasetError as exc:
                logger.warning("Langfuse dataset push skipped: %s", exc)
        dataset = client.dataset(name)
        if dataset is None:
            provenance = self.folder.metadata.get("provenance") or {}
            dataset_id = client.ensure_dataset(
                name,
                {
                    "source_path": str(self.folder.metadata.get("tasks_path") or ""),
                    "tasks_sha256": batch_hash,
                    "git_commit": (provenance.get("git") or {}).get("commit"),
                },
            )
        else:
            dataset_id = str(dataset["id"])
        known = {str(item.get("id")) for item in client.dataset_items(name)}
        for scenario, record in self.folder.items.items():
            if item_id(name, scenario) not in known:
                # The scenario left the set before it was ever pushed: an archived item keeps the
                # experiment from pointing at nothing.
                client.upsert_dataset_item(item_body(name, record, status=ARCHIVED))
        row = journaled_version(journal, host=client.host, project_id=project_id, dataset=name, content_hash=batch_hash)
        return {
            "name": name,
            "id": dataset_id,
            "version": row.get("version") if row else None,
            "version_matched": row is not None,
        }

    def _baseline(self) -> str:
        """The previous comparable batch next to this one (``""`` when there is none)."""

        metas: list[dict[str, Any]] = []
        for path in sorted(self.folder.path.parent.glob(f"*/{BATCH_FILE_NAME}")):
            if path.parent == self.folder.path:
                continue
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict):
                metas.append(meta)
        previous = previous_comparable(self.folder.metadata, metas, meta_of=lambda meta: meta)
        return str(previous.get("batch_id") or "") if previous else ""

    # -- bookkeeping -------------------------------------------------------------------------

    def fail(self, exc: LangfuseError) -> None:
        self.ledger.data["last_error"] = redact_json_safe(str(exc))
        self.ledger.data["state"] = PENDING
        self._save()

    def finish(self) -> SyncReport:
        scores = self.ledger.data.get("scores") or {}
        done = self._all_exported() and scores.get("state") == "posted" and not scores.get("rejected")
        if done:
            self.ledger.data.update(state=COMPLETE, last_error=None)
        else:
            self.ledger.data["state"] = PENDING
        self._save()
        return self.report()

    def report(self) -> SyncReport:
        items = self.ledger.items
        states = [items.get(str(row.get("scenario_id")), {}).get("state", PENDING) for row in self.folder.rows]
        exported = states.count(EXPORTED)
        unknown = states.count(UNKNOWN) + states.count(SENDING)
        pending = len(states) - exported - unknown
        data = self.ledger.data
        scores = data.get("scores") or {}
        if data.get("state") == COMPLETE:
            state: SyncState = "complete"
        elif exported or unknown:
            state = "partial"
        elif data.get("last_error"):
            state = "pending"
        else:
            state = "partial"
        return SyncReport(
            state=state,
            experiment_url=(data.get("experiment") or {}).get("url"),
            exported=exported,
            unknown=unknown,
            pending=pending,
            scores_accepted=int(scores.get("accepted") or 0),
            scores_rejected=len(scores.get("rejected") or []),
            enqueued=len((data.get("triage") or {}).get("enqueued") or []),
            error=data.get("last_error"),
        )

    def _save(self) -> None:
        self.ledger.save(retry_after_seconds=self.platform.retry_backoff_seconds, now=self.now())


def _report_from(ledger: SyncLedger, state: SyncState) -> SyncReport:
    items = ledger.items
    scores = ledger.data.get("scores") or {}
    return SyncReport(
        state=state,
        experiment_url=(ledger.data.get("experiment") or {}).get("url"),
        exported=sum(1 for item in items.values() if item.get("state") == EXPORTED),
        scores_accepted=int(scores.get("accepted") or 0),
        scores_rejected=len(scores.get("rejected") or []),
    )


def _source_path(metadata: Mapping[str, Any], repo_tasks: Path) -> str:
    path = str(metadata.get("tasks_path") or "")
    return path.replace("\\", "/") if path and not Path(path).is_absolute() else Path(repo_tasks).as_posix()


__all__ = ["LedgerLocked", "SyncReport", "SyncUsageError", "push_batch"]
