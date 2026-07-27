from __future__ import annotations

from argparse import Namespace
from typing import Any

import pytest

from src.cli.task_runner import run_task


class StreamingHarness:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = state
        self.streamed = False
        self.ran = False

    async def stream_updates(self, task: str, config: dict[str, Any]):
        self.streamed = True
        yield {"plan": {"task": task}}
        yield {"agent": {"final_answer": f"stream fallback: {task}", "config": config}}

    async def run(self, task: str, config: dict[str, Any]):
        self.ran = True
        return {"final_answer": f"run: {task}", "config": config}

    async def get_state_values(self, config: dict[str, Any]):
        _ = config
        return self.state


@pytest.mark.asyncio
async def test_run_task_streams_even_when_state_output_is_hidden(capsys) -> None:
    harness = StreamingHarness(state={"final_answer": "checkpoint result"})
    args = Namespace(show_state=False, hide_snapshot=False, json=False)

    result = await run_task(harness, "inspect page", args, {"recursion_limit": 5})

    output = capsys.readouterr().out
    assert result == {"final_answer": "checkpoint result"}
    assert "checkpoint result" in output
    assert "[PLAN]" not in output
    assert harness.streamed is True
    assert harness.ran is False


@pytest.mark.asyncio
async def test_run_task_falls_back_to_final_stream_update(capsys) -> None:
    harness = StreamingHarness(state=None)
    args = Namespace(show_state=False, hide_snapshot=False, json=False)

    result = await run_task(harness, "inspect page", args, {})

    assert result["final_answer"] == "stream fallback: inspect page"
    assert "stream fallback: inspect page" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_task_show_state_keeps_existing_step_output(capsys) -> None:
    harness = StreamingHarness(state={"final_answer": "checkpoint result"})
    args = Namespace(show_state=True, hide_snapshot=False, json=False)

    result = await run_task(harness, "inspect page", args, {})

    output = capsys.readouterr().out
    assert result == {"agent": {"final_answer": "stream fallback: inspect page", "config": {}}}
    assert "[PLAN]" in output
    assert "[AGENT]" in output
