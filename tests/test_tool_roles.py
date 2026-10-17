"""Tool roles: the engine keeps or drops the snapshot only through injected roles."""

from __future__ import annotations

from src.agent_loop.execution.observation import BrowserStateReducer, ToolResultNormalizer
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.execution.state import LoopState
from src.browser.roles import BrowserToolRoles
from src.harness.runtime import BrowserHarness
from src.harness.tools import NullToolRoles

PAGE = '- link "Jacket" ref=e3'


def after(name: str, *, content: str = "", status: str = "success") -> LoopState:
    return LoopState().apply(
        {
            "snapshot": "old page",
            "tool_request": {"name": name, "args": {}},
            "tool_result": {"name": name, "status": status, "content": content, "error": ""},
        }
    )


def reduce(state: LoopState, roles, read_only: frozenset[str] = frozenset()) -> dict:
    norm = ToolResultNormalizer(roles).normalize(state)
    return BrowserStateReducer(read_only).reduce(state, norm).updates


def test_browser_roles_keep_the_snapshot_and_drop_it_after_an_action() -> None:
    roles = BrowserToolRoles()

    assert reduce(after("browser_snapshot", content=PAGE), roles) == {"snapshot": PAGE}
    assert reduce(after("browser_click"), roles) == {"snapshot": ""}


def test_read_only_and_failed_calls_keep_the_snapshot() -> None:
    roles = BrowserToolRoles()

    assert reduce(after("browser_tabs"), roles, frozenset({"browser_tabs"})) == {}
    assert reduce(after("browser_click", status="error"), roles) == {}
    assert reduce(after("browser_snapshot", status="error"), roles) == {}


def test_without_roles_the_engine_neither_keeps_nor_drops_a_snapshot() -> None:
    roles = NullToolRoles()

    assert reduce(after("browser_snapshot", content=PAGE), roles) == {}
    assert reduce(after("browser_click"), roles) == {}
    assert reduce(after("browser_snapshot", content=PAGE), None) == {}


def test_engine_resources_default_to_no_roles() -> None:
    resources = EngineResources.from_harness(BrowserHarness(), llm=object())

    assert isinstance(resources.tool_roles, NullToolRoles)


def test_engine_resources_take_the_given_roles() -> None:
    roles = BrowserToolRoles()

    resources = EngineResources.from_harness(BrowserHarness(), llm=object(), tool_roles=roles)

    assert resources.tool_roles is roles
