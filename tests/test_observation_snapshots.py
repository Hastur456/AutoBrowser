"""Kept snapshots: per tool server, from the MCP ``readOnlyHint`` — never from a tool name."""

from __future__ import annotations

from src.agent_loop.execution.observation import BrowserStateReducer, ToolResultNormalizer
from src.agent_loop.execution.state import BrowserState, LoopState

PAGE = '- link "Jacket" ref=e3'


def after(
    name: str,
    *,
    content: str = "",
    status: str = "success",
    snapshots: dict[str, str] | None = None,
) -> LoopState:
    return LoopState(browser=BrowserState(snapshots=dict(snapshots or {}))).apply(
        {
            "tool_request": {"name": name, "args": {}},
            "tool_result": {"name": name, "status": status, "content": content, "error": ""},
        }
    )


def reduce(state: LoopState, *, server: str, read_only: bool) -> dict:
    norm = ToolResultNormalizer().normalize(state, server=server, read_only=read_only)
    return BrowserStateReducer().reduce(state, norm).updates


def test_a_read_only_call_keeps_its_output_as_the_servers_snapshot() -> None:
    state = after("anything", content=PAGE)

    assert reduce(state, server="web", read_only=True) == {"snapshots": {"web": PAGE}}


def test_a_state_changing_call_drops_only_its_own_servers_snapshot() -> None:
    state = after("anything", snapshots={"web": PAGE, "fs": "a.txt"})

    assert reduce(state, server="web", read_only=False) == {"snapshots": {"fs": "a.txt"}}


def test_another_servers_read_only_call_leaves_the_browser_snapshot_alone() -> None:
    state = after("memory_view", content="notes", snapshots={"web": PAGE})

    assert reduce(state, server="memory", read_only=True) == {
        "snapshots": {"web": PAGE, "memory": "notes"}
    }


def test_failed_calls_and_calls_without_a_snapshot_change_nothing() -> None:
    assert reduce(after("x", status="error", snapshots={"web": PAGE}), server="web", read_only=False) == {}
    assert reduce(after("x", status="error"), server="web", read_only=True) == {}
    assert reduce(after("x"), server="web", read_only=False) == {}


def test_tool_names_play_no_part() -> None:
    # The same name gives opposite results depending only on the annotation.
    state = after("browser_snapshot", content=PAGE, snapshots={"web": "old"})

    assert reduce(state, server="web", read_only=True) == {"snapshots": {"web": PAGE}}
    assert reduce(state, server="web", read_only=False) == {"snapshots": {}}
