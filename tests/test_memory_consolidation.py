"""MemoryConsolidator: proposals pass the policy, land unverified, and failures never raise."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.prompts import MEMORY_CONSOLIDATION_SYSTEM_PROMPT
from src.browser.memory import BrowserMemoryPolicy
from src.config import MemorySettings
from src.harness.memory_consolidation import MemoryConsolidator
from src.harness.memory_store import MemoryStore
from src.llm import ModelResponse


class Model:
    def __init__(self, content: str = "", *, delay: float = 0.0, error: Exception | None = None) -> None:
        self.content = content
        self.delay = delay
        self.error = error
        self.messages: list[Any] = []

    async def complete(self, messages: Any, **_kwargs: Any) -> ModelResponse:
        self.messages = list(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return ModelResponse(content=self.content, finish_reason="stop")


def answer(*entries: dict[str, Any]) -> str:
    return "Here you go:\n" + json.dumps({"entries": list(entries)})


def setup(
    tmp_path: Path, model: Model, *, timeout: float = 5.0, **fields: Any
) -> tuple[MemoryConsolidator, MemoryStore, list]:
    events: list = []
    store = MemoryStore(
        tmp_path, MemorySettings(persistent_enabled=True, **fields), policy=BrowserMemoryPolicy()
    )
    consolidator = MemoryConsolidator(
        model,
        store,
        timeout_seconds=timeout,
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    return consolidator, store, events


async def consolidate(consolidator: MemoryConsolidator) -> list[str]:
    return await consolidator.consolidate(
        task_id="task-7",
        task="Find a kettle on Ozon",
        final_answer="Kettle A, 1990 ₽",
        action_history="1. browser_navigate {\"url\": \"https://ozon.ru\"} -> success: ok",
        domains=["ozon.ru", ""],
    )


@pytest.mark.asyncio
async def test_good_proposals_are_written_unverified(tmp_path: Path) -> None:
    model = Model(answer({"path": "sites/ozon.ru.md", "description": "Ozon search", "body": "Use /search/?text=<q>."}))
    consolidator, store, events = setup(tmp_path, model)

    assert await consolidate(consolidator) == ["sites/ozon.ru.md"]

    entry = store.get("sites/ozon.ru.md")
    assert (entry.status, entry.source) == ("unverified", "agent:task-7")
    assert events == [("memory.consolidated", {"task_id": "task-7", "written": ["sites/ozon.ru.md"], "rejected": []})]
    system, user = model.messages
    assert system.content == MEMORY_CONSOLIDATION_SYSTEM_PROMPT
    assert "Sites visited:\nozon.ru" in user.content
    assert "browser_navigate" in user.content


@pytest.mark.asyncio
async def test_proposals_that_break_the_policy_are_rejected(tmp_path: Path) -> None:
    model = Model(
        answer(
            {"path": "sites/a.com.md", "description": "a", "body": "Click ref=e5."},
            {"path": "../escape.md", "description": "b", "body": "ok"},
            {"path": "sites/b.com.md", "description": "b", "body": "Fine."},
        )
    )
    consolidator, store, events = setup(tmp_path, model)

    assert await consolidate(consolidator) == ["sites/b.com.md"]
    rejected = events[0][1]["rejected"]
    assert [item["path"] for item in rejected] == ["sites/a.com.md", "../escape.md"]
    assert [entry.path for entry in store.entries()] == ["sites/b.com.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [MemorySettings().consolidation_max_entries, 1])
async def test_at_most_consolidation_max_entries_are_written(tmp_path: Path, limit: int) -> None:
    proposals = [{"path": f"sites/s{i}.com.md", "description": "d", "body": "b"} for i in range(5)]
    model = Model(answer(*proposals))
    consolidator, store, _ = setup(tmp_path, model, consolidation_max_entries=limit)

    assert len(await consolidate(consolidator)) == limit
    assert len(store.entries()) == limit
    assert model.messages[1].content.endswith(f"Entry limit:\n{limit}")


@pytest.mark.asyncio
async def test_prompt_fields_are_cut_to_consolidation_field_chars(tmp_path: Path) -> None:
    model = Model(answer())
    consolidator, _, _ = setup(tmp_path, model, consolidation_field_chars=20)

    await consolidator.consolidate(
        task_id="task-7", task="t" * 100, final_answer="a", action_history="h", domains=[]
    )

    assert "t" * 21 not in model.messages[1].content
    assert "t" * 20 + " [truncated]" in model.messages[1].content


@pytest.mark.asyncio
async def test_the_model_sees_the_current_bodies_of_the_visited_sites(tmp_path: Path) -> None:
    (tmp_path / "sites").mkdir()
    (tmp_path / "sites/ozon.ru.md").write_text(
        "---\nstatus: unverified\nsource: agent:t\ndescription: Ozon search\n---\n"
        "Search URL: https://www.ozon.ru/search/?text=<query>\n",
        encoding="utf-8",
    )
    (tmp_path / "sites/other.com.md").write_text(
        "---\nstatus: unverified\nsource: agent:t\ndescription: other\n---\nNot visited.\n",
        encoding="utf-8",
    )
    model = Model(answer())
    consolidator, _, _ = setup(tmp_path, model)

    await consolidate(consolidator)

    user = model.messages[1].content
    entries = user.split("Current entries for the sites visited:\n", 1)[1]
    assert entries.startswith("### sites/ozon.ru.md [unverified] — Ozon search\nSearch URL: https://")
    assert "Not visited." not in user


@pytest.mark.asyncio
async def test_without_entries_for_the_visited_sites_the_section_says_none(tmp_path: Path) -> None:
    model = Model(answer())
    consolidator, _, _ = setup(tmp_path, model)

    await consolidate(consolidator)

    assert "Current entries for the sites visited:\n(none)" in model.messages[1].content


@pytest.mark.asyncio
async def test_a_rewrite_keeps_the_trust_so_the_entry_can_still_be_promoted(tmp_path: Path) -> None:
    """record_outcome counts the success, then consolidation merges into the same file."""

    (tmp_path / "sites").mkdir()
    (tmp_path / "sites/ozon.ru.md").write_text(
        "---\nstatus: unverified\nsource: agent:t\ndescription: Ozon\n---\nSearch URL works.\n",
        encoding="utf-8",
    )
    merged = {"path": "sites/ozon.ru.md", "description": "Ozon", "body": "Search URL works.\nFilters need Enter."}
    consolidator, store, _ = setup(tmp_path, Model(answer(merged)), promote_after_successes=2)

    store.note_loaded("task-6", ["sites/ozon.ru.md"])
    store.record_outcome("task-6", "done")
    await consolidate(consolidator)
    entry = store.get("sites/ozon.ru.md")
    assert (entry.status, entry.uses, entry.body) == ("unverified", 1, merged["body"])

    store.note_loaded("task-8", ["sites/ozon.ru.md"])
    store.record_outcome("task-8", "done")
    assert store.get("sites/ozon.ru.md").status == "verified"


@pytest.mark.asyncio
async def test_rejection_reasons_are_cut_to_event_reason_chars(tmp_path: Path) -> None:
    model = Model(answer({"path": "x" * 50, "description": "d", "body": "b"}))
    consolidator, _, events = setup(tmp_path, model, event_reason_chars=10)

    await consolidate(consolidator)

    (rejected,) = events[0][1]["rejected"]
    assert len(rejected["path"]) == 10
    assert len(rejected["reason"]) == 10


@pytest.mark.asyncio
async def test_confirmed_entries_are_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / "sites").mkdir()
    (tmp_path / "sites/ozon.ru.md").write_text(
        "---\nstatus: verified\nsource: agent:t\ndescription: old\n---\nOld.\n", encoding="utf-8"
    )
    model = Model(answer({"path": "sites/ozon.ru.md", "description": "new", "body": "New."}))
    consolidator, store, events = setup(tmp_path, model)

    assert await consolidate(consolidator) == []
    assert store.get("sites/ozon.ru.md").body == "Old."
    assert "verified" in events[0][1]["rejected"][0]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        Model("no json at all"),
        Model('{"entries": "nope"}'),
        Model(error=RuntimeError("provider down")),
        Model(answer(), delay=1.0),
    ],
    ids=["no-json", "bad-shape", "error", "timeout"],
)
async def test_failures_emit_an_event_and_never_raise(tmp_path: Path, model: Model) -> None:
    consolidator, store, events = setup(tmp_path, model, timeout=0.05)

    assert await consolidate(consolidator) == []
    assert [kind for kind, _ in events] == ["memory.consolidation_failed"]
    assert store.entries() == ()
