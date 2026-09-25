"""MCP Manager tests against a real stdio MCP server (tests/mcp_fixtures/fake_server.py).

No pytest-asyncio dependency: every test drives its own event loop via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import McpError
from pydantic import ValidationError

from src.mcp import (
    AmbiguousResourceError,
    ConnectionState,
    MCPManager,
    RequestTimeoutError,
    ServerClosedError,
    ServerConnectionLostError,
    ServerRegistry,
    ServerStateLostError,
    ServerUnavailableError,
    UnknownToolError,
    parse_server_configs,
    qualify,
    validate_server_name,
)
from src.mcp.config import expand_env_strict

FAKE_SERVER = str(Path(__file__).parent / "mcp_fixtures" / "fake_server.py")


def fake(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [FAKE_SERVER],
        "init_timeout_s": 20,
        "reconnect": {"base_delay_s": 0.05, "max_attempts": 3},
    }
    config.update(overrides)
    return config


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=60))


async def wait_until(predicate: Any, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


def text(result: Any) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


# --------------------------------------------------------------------------- pure units


def test_server_name_validation() -> None:
    for good in ("playwright", "fs", "internal_search", "a-b_c"):
        validate_server_name(good)
    for bad in ("", "a__b", "_a", "a_", "a.b", "x" * 33, "a:b"):
        with pytest.raises(ValueError):
            validate_server_name(bad)


def test_qualify_sanitizes_truncates_and_disambiguates() -> None:
    taken: dict[str, Any] = {}
    first = qualify("srv", "get_user", taken)
    assert first == "srv__get_user"
    taken[first] = 1
    second = qualify("srv", "get.user", taken)  # sanitizes to the same base -> hashed
    assert second != first and second.startswith("srv__get_user_") and len(second) <= 64
    assert qualify("srv", "get.user", taken) == second  # deterministic
    long_name = qualify("srv", "x" * 200, {})
    assert len(long_name) == 64


def test_env_expansion_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOKEN", "s3cr3t")
    assert expand_env_strict("Bearer ${MCP_TOKEN}") == "Bearer s3cr3t"
    monkeypatch.delenv("MCP_TOKEN")
    with pytest.raises(ValueError):
        parse_server_configs(
            {"remote": {"transport": "streamable_http", "url": "http://x", "headers": {"A": "${MCP_TOKEN}"}}}
        )


def test_config_is_a_discriminated_union() -> None:
    parsed = parse_server_configs(
        {
            "a": {"transport": "stdio", "command": "npx"},
            "b": {"transport": "streamable_http", "url": "http://localhost:9000/mcp"},
        }
    )
    assert type(parsed["a"]).__name__ == "StdioServerConfig"
    assert type(parsed["b"]).__name__ == "StreamableHttpServerConfig"
    with pytest.raises(ValidationError):
        parse_server_configs({"c": {"transport": "sse", "url": "http://x"}})
    with pytest.raises(ValueError):
        ServerRegistry.from_mapping({"bad__name": {"transport": "stdio", "command": "x"}})


# --------------------------------------------------------------------------- lifecycle


def test_start_discovery_routing_and_shutdown_in_other_task() -> None:
    async def scenario() -> None:
        manager = MCPManager({"fake": fake(stateful=True)})
        # start and shutdown from *different* tasks: contexts live in owner tasks
        await asyncio.create_task(manager.start())
        status = manager.status()["fake"]
        assert status.state is ConnectionState.READY and status.generation == 1
        names = {t.qualified_name for t in manager.list_tools()}
        assert {"fake__echo", "fake__increment", "fake__add_tool"} <= names
        dotted = [t for t in manager.list_tools() if t.local_name == "get.user"]
        assert dotted and dotted[0].qualified_name == "fake__get_user"

        assert text(await manager.call_tool("fake__echo", {"text": "hi"})) == "hi"
        failing = await manager.call_tool("fake__fail", {"message": "boom"})
        assert failing.isError and "boom" in text(failing)
        with pytest.raises(UnknownToolError):
            await manager.call_tool("fake__nope", {})
        with pytest.raises(McpError):  # protocol-level error passes through unchanged
            await manager.get_prompt("fake__greet", {})
        assert (await manager.read_resource("memo://notes")).contents[0].text == "hello notes"

        owner = manager._servers["fake"].owner_task
        await asyncio.create_task(manager.shutdown())
        assert owner is not None and owner.done() and not owner.cancelled()
        assert manager.status()["fake"].state is ConnectionState.DISCONNECTED
        with pytest.raises(ServerClosedError):
            await manager.call_tool("fake__echo", {"text": "after"})

    run(scenario())


def test_partial_start_and_hanging_initialize() -> None:
    async def scenario() -> None:
        hang = {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-c", "import time; time.sleep(60)"],
            "init_timeout_s": 1,
        }
        missing = {"transport": "stdio", "command": "/nonexistent/binary-xyz", "init_timeout_s": 5}
        manager = MCPManager({"ok": fake(), "hang": hang, "missing": missing})
        started = time.monotonic()
        await manager.start()
        assert time.monotonic() - started < 15
        status = manager.status()
        assert status["ok"].state is ConnectionState.READY
        assert status["hang"].state is ConnectionState.FAILED
        assert status["missing"].state is ConnectionState.FAILED
        assert status["hang"].last_error
        assert all(t.server == "ok" for t in manager.list_tools())
        await manager.shutdown()

    run(scenario())


def test_timeout_is_typed_and_connection_survives() -> None:
    async def scenario() -> None:
        async with MCPManager({"fake": fake(call_timeout_s=0.5)}) as manager:
            with pytest.raises(RequestTimeoutError):
                await manager.call_tool("fake__slow", {"seconds": 3})
            await asyncio.sleep(0.3)  # liveness ping runs in the background
            assert manager.status()["fake"].state is ConnectionState.READY
            assert manager.status()["fake"].generation == 1
            assert text(await manager.call_tool("fake__echo", {"text": "still"})) == "still"

    run(scenario())


def test_crash_during_call_is_not_retried_and_reconnects_with_new_generation() -> None:
    async def scenario() -> None:
        async with MCPManager({"fake": fake(stateful=True)}) as manager:
            assert text(await manager.call_tool("fake__increment")) == "1"
            assert text(await manager.call_tool("fake__increment")) == "2"
            with pytest.raises(ServerConnectionLostError) as info:
                await manager.call_tool("fake__crash")
            assert info.value.generation == 1 and info.value.stateful is True
            await wait_until(lambda: manager.status()["fake"].state is ConnectionState.READY)
            assert manager.status()["fake"].generation == 2
            # server state is gone: the counter restarted in the new process
            assert text(await manager.call_tool("fake__increment")) == "1"
            with pytest.raises(ServerStateLostError):
                await manager.call_tool("fake__increment", expected_generation=1)

    run(scenario())


def test_silent_server_exit_is_detected_without_a_request() -> None:
    async def scenario() -> None:
        async with MCPManager({"fake": fake(stateful=True)}) as manager:
            await manager.call_tool("fake__exit_later", {"delay": 0.2})
            await wait_until(lambda: manager.status()["fake"].generation == 2)
            assert manager.status()["fake"].state is ConnectionState.READY

    run(scenario())


def test_reconnect_disabled_goes_failed_then_recovers_lazily() -> None:
    async def scenario() -> None:
        async with MCPManager({"fake": fake(reconnect={"enabled": False})}) as manager:
            with pytest.raises(ServerConnectionLostError):
                await manager.call_tool("fake__crash")
            await wait_until(lambda: manager.status()["fake"].state is ConnectionState.FAILED)
            assert manager.list_tools() == []  # not offered to the LLM while down
            # ... but still routable: a call triggers recovery
            assert text(await manager.call_tool("fake__echo", {"text": "back"})) == "back"
            assert manager.status()["fake"].generation == 2

    run(scenario())


def test_reconnect_exhaustion_ends_in_failed() -> None:
    async def scenario() -> None:
        async with MCPManager({"fake": fake()}) as manager:
            # make every further attempt fail
            managed = manager._servers["fake"]
            managed.config = managed.config.model_copy(update={"command": "/nonexistent/binary-xyz"})
            with pytest.raises(ServerConnectionLostError):
                await manager.call_tool("fake__crash")
            await wait_until(lambda: manager.status()["fake"].state is ConnectionState.FAILED)
            assert manager.status()["fake"].reconnect_attempts == 3
            with pytest.raises(ServerUnavailableError):
                await manager.call_tool("fake__echo", {"text": "x"})

    run(scenario())


def test_list_changed_triggers_rediscovery() -> None:
    async def scenario() -> None:
        async with MCPManager({"fake": fake()}) as manager:
            version = manager.catalog_version
            assert text(await manager.call_tool("fake__add_tool", {"name": "late"})) == "added"
            await wait_until(lambda: any(t.qualified_name == "fake__late" for t in manager.list_tools()))
            assert manager.catalog_version > version
            assert text(await manager.call_tool("fake__late")) == "dynamic:late"

    run(scenario())


def test_shutdown_cancels_inflight_and_is_bounded() -> None:
    async def scenario() -> None:
        manager = MCPManager({"fake": fake(call_timeout_s=30)})
        await manager.start()
        call = asyncio.create_task(manager.call_tool("fake__slow", {"seconds": 20}))
        await asyncio.sleep(0.3)
        started = time.monotonic()
        await manager.shutdown(timeout=1.0)
        assert time.monotonic() - started < 8
        with pytest.raises(ServerClosedError):
            await call

    run(scenario())


def test_shutdown_during_reconnect_backoff_is_immediate() -> None:
    async def scenario() -> None:
        manager = MCPManager({"fake": fake(reconnect={"base_delay_s": 10, "max_attempts": 5})})
        await manager.start()
        with pytest.raises(ServerConnectionLostError):
            await manager.call_tool("fake__crash")
        assert manager.status()["fake"].state is ConnectionState.RECONNECTING
        started = time.monotonic()
        await manager.shutdown(timeout=2.0)
        assert time.monotonic() - started < 6
        assert manager.status()["fake"].state is ConnectionState.DISCONNECTED

    run(scenario())


def test_concurrent_calls_on_failed_server_connect_once() -> None:
    async def scenario() -> None:
        manager = MCPManager({"fake": fake()})
        await manager.start()
        await manager.reconnect("fake")  # generation 2
        managed = manager._servers["fake"]
        await manager._close(managed, 2.0)
        managed.closing = False
        results = await asyncio.gather(*(manager.call_tool("fake__echo", {"text": str(i)}) for i in range(5)))
        assert [text(r) for r in results] == [str(i) for i in range(5)]
        assert manager.status()["fake"].generation == 3  # exactly one new connection
        await manager.shutdown()

    run(scenario())


def test_add_remove_server_and_resource_ambiguity() -> None:
    async def scenario() -> None:
        async with MCPManager({"one": fake()}) as manager:
            await manager.add_server("two", fake())
            assert {"one__echo", "two__echo"} <= {t.qualified_name for t in manager.list_tools()}
            with pytest.raises(AmbiguousResourceError):
                await manager.read_resource("memo://notes")
            contents = (await manager.read_resource("memo://notes", server="two")).contents
            assert contents[0].text == "hello notes"
            await manager.remove_server("two")
            assert all(t.server == "one" for t in manager.list_tools())
            assert (await manager.read_resource("memo://notes")).contents[0].text == "hello notes"

    run(scenario())


def test_ephemeral_mode_has_no_state_between_calls() -> None:
    async def scenario() -> None:
        async with MCPManager({"eph": fake(connection_mode="ephemeral")}) as manager:
            assert manager.status()["eph"].state is ConnectionState.READY
            assert text(await manager.call_tool("eph__increment")) == "1"
            assert text(await manager.call_tool("eph__increment")) == "1"
            with pytest.raises(ServerConnectionLostError):
                await manager.call_tool("eph__crash")
            assert text(await manager.call_tool("eph__echo", {"text": "ok"})) == "ok"

    run(scenario())


def test_streamable_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import socket
    import subprocess

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "FAKE_SERVER_TRANSPORT": "streamable-http",
        "FAKE_SERVER_PORT": str(port),
    }
    process = subprocess.Popen([sys.executable, FAKE_SERVER], env=env)
    monkeypatch.setenv("FAKE_MCP_TOKEN", "t0ken")

    async def scenario() -> None:
        for _ in range(100):  # wait for the HTTP server to listen
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            await asyncio.sleep(0.1)
        config = {
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{port}/mcp",
            "headers": {"Authorization": "Bearer ${FAKE_MCP_TOKEN}"},
        }
        async with MCPManager({"remote": config}) as manager:
            assert manager.status()["remote"].state is ConnectionState.READY
            assert text(await manager.call_tool("remote__echo", {"text": "http"})) == "http"
            assert text(await manager.call_tool("remote__add_tool", {"name": "late"})) == "added"

    try:
        run(scenario())
    finally:
        process.terminate()
        process.wait(timeout=10)
