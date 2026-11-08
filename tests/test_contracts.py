"""Tests for the neutral contracts leaf (:mod:`src.contracts`)."""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from src.contracts import (
    LADDER_ORDER,
    HookEvent,
    HookResult,
    PlaybackAdapter,
    RunBudget,
    RunProfile,
    StepSource,
    StepSourceFactory,
    TaskRequest,
    TaskUsage,
    ladder_rank,
)
from src.llm import ModelPrice, ModelTiers, ModelUsage

CONTRACTS_PATH = Path(__file__).resolve().parents[1] / "src" / "contracts.py"


def test_hook_event_is_json_serializable() -> None:
    event = HookEvent(
        name="stop",
        session_id="session-1",
        goal_id="task-1",
        task_id="task-1",
        task="find 3 jackets",
        tool="browser_navigate",
        server="playwright",
        args={"url": "https://example.com", "nested": {"n": [1, 2]}},
        result={"name": "browser_navigate", "status": "success", "content": "ok"},
        reason="needs approval",
        final_answer="Jacket A costs 1 299 ₽",
        evidence=("snapshot text", "observation text"),
        stop_hook_active=True,
        status="done",
    )

    decoded = json.loads(json.dumps(asdict(event), ensure_ascii=False))

    assert decoded["name"] == "stop"
    assert decoded["evidence"] == ["snapshot text", "observation text"]
    assert decoded["args"]["nested"] == {"n": [1, 2]}


def test_hook_event_defaults_are_empty_and_independent() -> None:
    first = HookEvent(name="goal_start", session_id=None, goal_id="g", task_id="t", task="x")
    second = HookEvent(name="goal_start", session_id=None, goal_id="g", task_id="t", task="x")

    assert first.args == {} and first.result == {} and first.evidence == ()
    assert first.args is not second.args


def test_hook_result_defaults_mean_no_opinion() -> None:
    result = HookResult()

    assert result.decision is None
    assert result.updated_input is None
    assert result.updated_output is None
    assert result.additional_context == ""


def test_hook_contracts_are_frozen() -> None:
    with pytest.raises(AttributeError):
        HookResult().decision = "deny"  # type: ignore[misc]


def test_contracts_import_only_the_standard_library() -> None:
    """``src/contracts.py`` is a neutral leaf every layer may depend on."""

    tree = ast.parse(CONTRACTS_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not allowed in src/contracts.py"
            imported.add(str(node.module).split(".")[0])

    allowed = set(sys.stdlib_module_names) | {"__future__"}
    assert imported <= allowed, f"non-stdlib imports: {sorted(imported - allowed)}"


def test_the_default_run_profile_changes_nothing() -> None:
    profile = TaskRequest(task="t", task_id="task-1").profile

    assert profile == RunProfile()
    assert profile.playback is None
    assert profile.max_stage == "human"
    assert profile.carry_context is True
    assert profile.record_as is None
    assert profile.budget == RunBudget()
    assert profile.allowed_domains == ()


def test_task_request_profiles_are_independent() -> None:
    first = TaskRequest(task="a", task_id="1")
    second = TaskRequest(task="b", task_id="2")

    assert first.profile.labels is not second.profile.labels


def test_the_ladder_is_ordered_cheapest_first() -> None:
    assert LADDER_ORDER == ("probe", "playback", "retarget", "agent", "human")
    assert [ladder_rank(stage) for stage in LADDER_ORDER] == [0, 1, 2, 3, 4]
    assert ladder_rank("unknown") > ladder_rank("human")


def test_task_usage_starts_empty_with_a_known_zero_cost() -> None:
    usage = TaskUsage()

    assert usage.model_calls == 0
    assert usage.tokens == 0
    assert usage.cost == 0.0
    assert usage.stage_reached == "agent"


def test_run_contracts_are_frozen() -> None:
    with pytest.raises(AttributeError):
        RunProfile().max_stage = "agent"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        TaskUsage().model_calls = 1  # type: ignore[misc]


def test_step_protocols_are_runtime_checkable() -> None:
    class Source:
        info: dict = {}
        usage: tuple = ()

        def plan(self):  # noqa: ANN201
            return None

        async def next(self, view):  # noqa: ANN001, ANN201
            raise NotImplementedError

        def observe(self, proposal, result, view, *, blocked=""):  # noqa: ANN001, ANN201
            return None

    assert isinstance(Source(), StepSource)
    assert not isinstance(object(), StepSourceFactory)
    assert not isinstance(object(), PlaybackAdapter)


def test_a_model_price_charges_cached_input_at_its_own_rate() -> None:
    price = ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0, cached_input_per_mtok=0.5)
    usage = ModelUsage(input_tokens=1_000_000, output_tokens=500_000, cached_input_tokens=400_000)

    # 600k fresh input * 2 + 400k cached * 0.5 + 500k output * 8, per million.
    assert price.cost(usage) == pytest.approx(1.2 + 0.2 + 4.0)


def test_a_model_price_without_cached_rate_charges_cached_as_input() -> None:
    price = ModelPrice(input_per_mtok=1.0, output_per_mtok=1.0)

    assert price.cost(ModelUsage(input_tokens=10, output_tokens=0, cached_input_tokens=5)) == (
        pytest.approx(10 / 1_000_000)
    )


@pytest.mark.parametrize(
    "usage",
    [None, ModelUsage(), ModelUsage(input_tokens=10), ModelUsage(output_tokens=10)],
)
def test_a_model_price_is_unknown_without_reported_tokens(usage: ModelUsage | None) -> None:
    assert ModelPrice(input_per_mtok=1.0, output_per_mtok=1.0).cost(usage) is None


def test_model_tiers_need_a_tier_and_a_valid_retarget_tier() -> None:
    model = object()
    tiers = ModelTiers.single(model, label="m")  # type: ignore[arg-type]

    assert tiers.tier(0).label == "m"
    assert tiers.tier(5).label == "m"  # clamped
    with pytest.raises(ValueError):
        ModelTiers(())
    with pytest.raises(ValueError):
        ModelTiers(tiers.tiers, retarget_tier=1)
