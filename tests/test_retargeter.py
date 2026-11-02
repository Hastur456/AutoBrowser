"""``Retargeter``: stage 2 accepts only one call of the recorded tool with live references."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.agent_loop.prompts import PLAYBACK_RETARGET_SYSTEM_PROMPT
from src.agent_loop.recordings import RecordedStep
from src.browser.playback import BrowserPlaybackAdapter
from src.config import ReplaySettings
from src.contracts import Binding, StepView, ToolDef
from src.harness.playback import Retargeter
from src.llm import ModelPrice, ModelProviderError, ModelResponse, ModelUsage
from src.messages import ToolCall

PAGE = """- Page URL: https://shop.example/item/1
- tablist:
  - tab "Description" [ref=e70]
  - tab "Characteristics" [ref=e71]"""
STEP = RecordedStep(
    index=3,
    tool="click",
    args={"element": "Specs tab", "target": "e21"},
    bindings=(Binding(path=("target",), value="e21", descriptor={"role": "tab", "name": "Specs", "context": ["tablist"]}),),
)
CLICK = ToolDef(name="click", description="Click an element", input_schema={"type": "object"})
VIEW = StepView(task="t", turn=1, tool_result={}, snapshots={}, tool_schemas={})


class Model:
    def __init__(self, response: Any = None, *, delay: float = 0.0, error: Exception | None = None) -> None:
        self.response = response
        self.delay = delay
        self.error = error
        self.seen: list[tuple[list[Any], list[ToolDef]]] = []

    async def complete(self, messages: Any, *, tools: Any = (), **params: Any) -> ModelResponse:
        self.seen.append((list(messages), list(tools)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.response


def calls(*pairs: tuple[str, dict[str, Any]]) -> ModelResponse:
    return ModelResponse(
        tool_calls=tuple(ToolCall(id=f"c{index}", name=name, arguments=args) for index, (name, args) in enumerate(pairs)),
        usage=ModelUsage(input_tokens=1000, output_tokens=20),
    )


async def retarget(model: Model, **settings: Any) -> tuple[Any, Any, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []

    async def tool_def(name: str) -> ToolDef | None:
        return CLICK if name == "click" else None

    retargeter = Retargeter(
        model,  # type: ignore[arg-type]
        BrowserPlaybackAdapter(ReplaySettings()),
        ReplaySettings(**settings),
        tool_def=tool_def,
        model_label="cheap-model",
        price=ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0),
    )
    args, record = await retargeter.retarget(
        task="Read the specs",
        step=STEP,
        reason="no tab \"Specs\" on the page",
        view=VIEW,
        page_texts=[PAGE],
        emit=lambda kind, payload: events.append((kind, dict(payload))),
    )
    return args, record, events


@pytest.mark.asyncio
async def test_one_call_with_a_live_reference_is_accepted() -> None:
    model = Model(calls(("click", {"element": "Characteristics tab", "target": "e71"})))

    args, record, events = await retarget(model)

    assert args == {"element": "Characteristics tab", "target": "e71"}
    assert (record.phase, record.model, record.usage) == ("retarget", "cheap-model", {"input_tokens": 1000, "output_tokens": 20})
    assert record.cost == pytest.approx((1000 * 1.0 + 20 * 2.0) / 1_000_000)
    assert [kind for kind, _ in events] == ["model.requested", "model.responded"]
    assert all(payload["phase"] == "retarget" for _, payload in events)


@pytest.mark.asyncio
async def test_the_model_sees_only_the_recorded_tool_and_the_described_step() -> None:
    model = Model(calls(("click", {"target": "e71"})))

    await retarget(model)

    ((messages, tools),) = model.seen
    assert [tool.name for tool in tools] == ["click"]
    assert messages[0].content == PLAYBACK_RETARGET_SYSTEM_PROMPT
    request = messages[1].content
    assert '"target": <element role="tab" name="Specs" context=["tablist"], was e21>' in request
    assert 'tab "Characteristics" [ref=e71]' in request
    assert 'no tab "Specs"' in request


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(content="There is no Specs tab on this page."),
        calls(("navigate", {"url": "https://elsewhere.example"})),
        calls(("click", {"target": "e71"}), ("click", {"target": "e70"})),
        calls(("click", {"target": "e999"})),  # an invented reference
        calls(("click", {"element": "Characteristics tab"})),  # the reference argument is missing
    ],
)
@pytest.mark.asyncio
async def test_anything_else_is_rejected_but_still_counted(response: ModelResponse) -> None:
    args, record, _ = await retarget(Model(response))

    assert args is None
    assert record is not None and record.phase == "retarget"


@pytest.mark.asyncio
async def test_a_slow_model_times_out() -> None:
    args, record, events = await retarget(Model(calls(("click", {"target": "e71"})), delay=1.0), retarget_timeout_seconds=0.05)

    assert args is None and record is not None and record.usage == {} and record.cost is None
    assert events[-1] == ("model.responded", {"phase": "retarget", "tier": 0, "error": "timeout"})


@pytest.mark.asyncio
async def test_a_provider_error_is_a_rejection() -> None:
    error = ModelProviderError("503 from upstream", provider="ollama", retryable=True)

    args, record, events = await retarget(Model(error=error))

    assert args is None and record is not None
    assert events[-1][1]["error"] == "503 from upstream"


@pytest.mark.asyncio
async def test_a_tool_that_is_gone_is_not_asked_about() -> None:
    model = Model(calls(("click", {"target": "e71"})))
    retargeter = Retargeter(
        model,  # type: ignore[arg-type]
        BrowserPlaybackAdapter(),
        ReplaySettings(),
        tool_def=lambda name: _none(),
    )

    args, record = await retargeter.retarget(
        task="t", step=STEP, reason="r", view=VIEW, page_texts=[PAGE], emit=lambda *_: None
    )

    assert (args, record) == (None, None) and model.seen == []


async def _none() -> None:
    return None


@pytest.mark.asyncio
async def test_the_page_text_is_cut_to_the_retarget_context() -> None:
    model = Model(calls(("click", {"target": "e71"})))
    long_page = PAGE + "\n" + "- paragraph \"filler\"\n" * 2_000

    async def tool_def(name: str) -> ToolDef:
        return CLICK

    retargeter = Retargeter(model, BrowserPlaybackAdapter(), ReplaySettings(retarget_context_chars=1_000), tool_def=tool_def)  # type: ignore[arg-type]
    await retargeter.retarget(task="t", step=STEP, reason="r", view=VIEW, page_texts=[long_page], emit=lambda *_: None)

    request = model.seen[0][0][1].content
    page = request.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
    assert len(page) == 1_000
