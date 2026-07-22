"""Command-line entrypoint for the AutoBrowser agent."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
import socket
import subprocess
from typing import Any

from dotenv import load_dotenv
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_ollama import ChatOllama
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.agent.agent import DEFAULT_OLLAMA_MODEL, build_agent_graph
from src.harness.runtime import BrowserHarness

load_dotenv()

DEFAULT_CDP_PORT = int(os.getenv("PORT", "9222"))
DEFAULT_CHROME_PATH = os.getenv(
    "CHROME_PATH",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
DEFAULT_USER_DATA_DIR = os.getenv("USER_DATA_DIR", r"C:\temp\chrome_debug_profile")
DEFAULT_LANGSMITH_PROJECT = "autobrowser"

_NODE_LABELS = {
    "plan": "PLAN",
    "agent": "AGENT",
    "policy": "POLICY",
    "human_input": "HUMAN",
    "executor": "EXECUTOR",
    "observe": "OBSERVE",
}

class MCPRuntime:
    """Keep the stdio MCP process and ClientSession alive while tools run."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def start(self) -> ClientSession:
        params = StdioServerParameters(
            command="npx",
            args=[
                "-y",
                "@playwright/mcp@latest",
                "--cdp-endpoint",
                f"http://localhost:{self.port}",
            ],
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.session = session
        return session

    async def close(self) -> None:
        try:
            await self._stack.aclose()
        except GeneratorExit:
            pass
        finally:
            self.session = None
            self._stack = AsyncExitStack()


_mcp_runtime: MCPRuntime | None = None


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""

    parser = argparse.ArgumentParser(
        description="Run the AutoBrowser LangGraph agent from the command line."
    )
    parser.add_argument("task", nargs="*", help="Task for the browser agent.")
    parser.add_argument("--task", "-t", dest="task_text", help="Task for the agent.")
    parser.add_argument(
        "--loop",
        "-l",
        action="store_true",
        help="Run interactive loop mode. Exit with quit, exit, or выход.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_OLLAMA_MODEL,
        help=f"Ollama model name. Default: {DEFAULT_OLLAMA_MODEL}",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="LLM temperature. Default: 0",
    )
    parser.add_argument(
        "--show-state",
        action="store_true",
        help="Print state updates after each graph node runs.",
    )
    parser.add_argument(
        "--hide-snapshot",
        action="store_true",
        help="Redact browser snapshots from printed state updates.",
    )
    parser.add_argument(
        "--show-tools",
        action="store_true",
        help="Print MCP tool names after loading them.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print state as JSON instead of a readable Python representation.",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Run without loading MCP tools. Useful for dry CLI checks.",
    )
    parser.add_argument(
        "--compress-tools",
        action="store_true",
        help="Compress tool outputs and snapshots with the observer LLM.",
    )
    parser.add_argument(
        "--chrome-path",
        default=DEFAULT_CHROME_PATH,
        help="Path to Chrome executable. Defaults to CHROME_PATH from .env.",
    )
    parser.add_argument(
        "--user-data-dir",
        default=DEFAULT_USER_DATA_DIR,
        help="Chrome user data directory. Defaults to USER_DATA_DIR from .env.",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=DEFAULT_CDP_PORT,
        help=f"Chrome DevTools Protocol port. Default: {DEFAULT_CDP_PORT}",
    )
    parser.add_argument(
        "--cdp-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for Chrome CDP port. Default: 30",
    )
    parser.add_argument(
        "--recursion-limit",
        type=int,
        default=50,
        help="LangGraph recursion limit. Default: 25",
    )
    return parser


def is_port_open(port: int) -> bool:
    """Return whether a localhost TCP port is accepting connections."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("localhost", port)) == 0


def start_chrome_cdp(
    chrome_path: str,
    user_data_dir: str,
    port: int,
) -> subprocess.Popen[Any] | None:
    """Start Chrome with CDP enabled unless the port is already open."""

    if is_port_open(port):
        return None

    if not chrome_path:
        raise RuntimeError("CHROME_PATH is not set. Pass --chrome-path or set it in .env.")
    if not user_data_dir:
        raise RuntimeError(
            "USER_DATA_DIR is not set. Pass --user-data-dir or set it in .env."
        )

    return subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    )


async def wait_for_port(port: int, timeout_seconds: float) -> None:
    """Wait until a localhost TCP port opens."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not is_port_open(port):
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Chrome CDP port {port} did not open in time.")
        print("Подключение к серверу...")
        await asyncio.sleep(0.5)


async def get_mcp_session(port: int) -> ClientSession:
    """Create or reuse a direct Playwright MCP ClientSession."""

    global _mcp_runtime
    if _mcp_runtime is None or _mcp_runtime.port != port:
        if _mcp_runtime is not None:
            await _mcp_runtime.close()
        os.environ["PORT"] = str(port)
        _mcp_runtime = MCPRuntime(port)
        return await _mcp_runtime.start()

    if _mcp_runtime.session is None:
        return await _mcp_runtime.start()
    return _mcp_runtime.session


async def close_mcp_session() -> None:
    """Close the direct MCP session and stdio server process."""

    global _mcp_runtime
    if _mcp_runtime is not None:
        await _mcp_runtime.close()
        _mcp_runtime = None


async def load_browser_tools(port: int) -> list[Any]:
    """Load Playwright MCP tools from a direct ClientSession."""

    os.environ["PORT"] = str(port)
    session = await get_mcp_session(port)
    return list(await load_mcp_tools(session))


def print_tools(tools: list[Any]) -> None:
    """Print loaded MCP tool names."""

    print("MCP tools:")
    if not tools:
        print("- none")
        return
    for tool in tools:
        print(f"- {getattr(tool, 'name', tool)}")


SNAPSHOT_REDACTION = "[browser_snapshot hidden]"


def _redact_snapshot_value(value: Any, *, parent_key: str = "") -> Any:
    """Return a terminal-safe copy with browser snapshots removed."""

    if parent_key == "snapshot":
        return SNAPSHOT_REDACTION

    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        is_snapshot_result = str(value.get("name", "") or "") == "browser_snapshot"
        has_snapshot_field = bool(str(value.get("snapshot", "") or "").strip())
        for key, item in value.items():
            key_text = str(key)
            if key_text == "snapshot":
                redacted[key] = SNAPSHOT_REDACTION
            elif key_text == "content" and is_snapshot_result:
                redacted[key] = SNAPSHOT_REDACTION
            elif key_text == "observation" and has_snapshot_field:
                redacted[key] = SNAPSHOT_REDACTION
            else:
                redacted[key] = _redact_snapshot_value(item, parent_key=key_text)
        return redacted

    if isinstance(value, list):
        return [_redact_snapshot_value(item, parent_key=parent_key) for item in value]

    return value


def format_state(value: Any, as_json: bool = False) -> str:
    """Format a graph state value for terminal output."""

    if as_json:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return repr(value)


def print_step(
    node_name: str,
    update: Any,
    as_json: bool = False,
    *,
    hide_snapshot: bool = False,
) -> None:
    """Print a compact node update after a graph step."""

    label = _NODE_LABELS.get(node_name, node_name.upper())
    printable_update = _redact_snapshot_value(update) if hide_snapshot else update
    if as_json:
        print(f"[{label}] {format_state(printable_update, as_json=True)}")
        return

    print(f"\n[{label}]")
    if not isinstance(printable_update, dict):
        print(format_state(printable_update))
        return

    for key in (
        "plan",
        "decision",
        "tool_request",
        "policy_decision",
        "tool_result",
        "observation",
        "snapshot",
        "refs",
        "last_tool",
        "last_args",
        "repeat_count",
        "final_answer",
        "error",
    ):
        value = printable_update.get(key)
        if value not in (None, "", [], {}):
            print(f"{key}: {format_state(value)}")


def configure_langsmith_tracing() -> bool:
    """Normalize LangSmith tracing environment variables.

    LangChain/LangGraph read tracing settings from environment variables. This
    keeps the supported LangSmith names and legacy LangChain names in sync so a
    project .env can use either convention.
    """

    tracing = os.getenv("LANGSMITH_TRACING") or os.getenv("LANGCHAIN_TRACING_V2")
    enabled = str(tracing).lower() in {"1", "true", "yes", "on"}

    if enabled:
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")

    project = (
        os.getenv("LANGSMITH_PROJECT")
        or os.getenv("LANGCHAIN_PROJECT")
        or DEFAULT_LANGSMITH_PROJECT
    )
    os.environ.setdefault("LANGSMITH_PROJECT", project)
    os.environ.setdefault("LANGCHAIN_PROJECT", project)

    return enabled


def _print_final_state(result: Any, as_json: bool) -> None:
    if as_json:
        print(format_state(result, as_json=True))
        return

    if isinstance(result, dict):
        if result.get("final_answer"):
            print(result["final_answer"])
            return
        if result.get("error"):
            print(f"Error: {result['error']}")
            return

    print(format_state(result))


async def run_agent(args: argparse.Namespace) -> int:
    """Run the agent with parsed CLI arguments."""

    tracing_enabled = configure_langsmith_tracing()
    llm = ChatOllama(model=args.model, temperature=args.temperature)
    mcp_enabled = not args.no_mcp
    try:
        if args.no_mcp:
            tools: list[Any] = []
        else:
            start_chrome_cdp(args.chrome_path, args.user_data_dir, args.cdp_port)
            await wait_for_port(args.cdp_port, args.cdp_timeout)
            print("=== Сервер подключен ===")
            tools = await load_browser_tools(args.cdp_port)
            if args.show_tools:
                print_tools(tools)

        harness = BrowserHarness(
            build_agent_graph,
            llm=llm,
            tools=tools,
            compress_tools=args.compress_tools,
        )
        config = {
            "recursion_limit": args.recursion_limit,
            "run_name": "AutoBrowser CLI task",
            "metadata": {
                "model": args.model,
                "temperature": args.temperature,
                "show_state": args.show_state,
                "hide_snapshot": args.hide_snapshot,
                "langsmith_tracing": tracing_enabled,
                "compress_tools": args.compress_tools,
            },
            "tags": [
                "autobrowser",
                "cli",
            ],
        }

        if args.loop:
            print("Интерактивный режим. Введите 'quit' для выхода.\n")
            while True:
                try:
                    task = input("Задача> ").strip()
                except (KeyboardInterrupt, EOFError):
                    print("\nВыход.")
                    return 0
                if not task:
                    continue
                if task.lower() in {"quit", "exit", "выход"}:
                    return 0
                await run_task(harness, task, args, config)
                print()
            return 0

        task = resolve_task(args)
        await run_task(harness, task, args, config)
        return 0
    finally:
        if mcp_enabled:
            await close_mcp_session()


def resolve_task(args: argparse.Namespace) -> str:
    """Resolve a task from positional args, --task, or stdin prompt."""

    task = " ".join(args.task).strip()
    if task:
        return task
    if args.task_text:
        return args.task_text.strip()
    return input("Задача> ").strip()


async def run_task(
    harness: BrowserHarness,
    task: str,
    args: argparse.Namespace,
    config: dict[str, Any],
) -> Any:
    """Run one task on an already built harness."""

    if args.show_state:
        final_update: Any = None
        async for chunk in harness.stream_updates(
            task,
            config=config,
        ):
            final_update = chunk
            for node_name, update in chunk.items():
                print_step(
                    node_name,
                    update,
                    args.json,
                    hide_snapshot=args.hide_snapshot,
                )

        if final_update is None:
            print("Agent finished without state updates.")
        return final_update

    result = await harness.run(task, config=config)
    _print_final_state(result, args.json)
    return result


def main() -> int:
    """Parse CLI arguments and run the async agent."""

    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(run_agent(args))


if __name__ == "__main__":
    raise SystemExit(main())
