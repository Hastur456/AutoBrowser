"""Command-line entrypoint for the AutoBrowser agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
from typing import Any

from dotenv import load_dotenv
from langchain_ollama import ChatOllama

from src.agent.agent import DEFAULT_OLLAMA_MODEL, build_agent_graph
from src.mcp.mcp_setup import setup_mcp

load_dotenv()

DEFAULT_CDP_PORT = int(os.getenv("PORT", "9222"))
DEFAULT_CHROME_PATH = os.getenv("CHROME_PATH", "")
DEFAULT_USER_DATA_DIR = os.getenv("USER_DATA_DIR", "")
DEFAULT_LANGSMITH_PROJECT = "autobrowser"

_NODE_LABELS = {
    "plan": "PLAN",
    "agent": "AGENT",
    "policy": "POLICY",
    "human_input": "HUMAN",
    "executor": "EXECUTOR",
    "observe": "OBSERVE",
}

_mcp_client = None


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
        default=25,
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
            "--remote-allow-origins=*",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-dev-shm-usage",
            "--no-sandbox",
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


async def get_mcp_client(port: int):
    global _mcp_client
    if _mcp_client is None:
        os.environ["PORT"] = str(port)
        _mcp_client = await setup_mcp()
    return _mcp_client


async def load_browser_tools(port: int) -> list[Any]:
    """Load all Playwright MCP tools for the selected CDP port."""

    os.environ["PORT"] = str(port)
    return list(await get_mcp_client(port))


def print_tools(tools: list[Any]) -> None:
    """Print loaded MCP tool names."""

    print("MCP tools:")
    if not tools:
        print("- none")
        return
    for tool in tools:
        print(f"- {getattr(tool, 'name', tool)}")


def format_state(value: Any, as_json: bool = False) -> str:
    """Format a graph state value for terminal output."""

    if as_json:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return repr(value)


def print_step(node_name: str, update: Any, as_json: bool = False) -> None:
    """Print a compact node update after a graph step."""

    label = _NODE_LABELS.get(node_name, node_name.upper())
    if as_json:
        print(f"[{label}] {format_state(update, as_json=True)}")
        return

    print(f"\n[{label}]")
    if not isinstance(update, dict):
        print(format_state(update))
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
        value = update.get(key)
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
    if args.no_mcp:
        tools: list[Any] = []
    else:
        start_chrome_cdp(args.chrome_path, args.user_data_dir, args.cdp_port)
        await wait_for_port(args.cdp_port, args.cdp_timeout)
        print("=== Сервер подключен ===")
        tools = await load_browser_tools(args.cdp_port)
        if args.show_tools:
            print_tools(tools)

    graph = build_agent_graph(llm=llm, tools=tools)
    config = {
        "recursion_limit": args.recursion_limit,
        "run_name": "AutoBrowser CLI task",
        "metadata": {
            "model": args.model,
            "temperature": args.temperature,
            "show_state": args.show_state,
            "langsmith_tracing": tracing_enabled,
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
            await run_task(graph, task, args, config)
            print()
        return 0

    task = resolve_task(args)
    await run_task(graph, task, args, config)
    return 0


def resolve_task(args: argparse.Namespace) -> str:
    """Resolve a task from positional args, --task, or stdin prompt."""

    task = " ".join(args.task).strip()
    if task:
        return task
    if args.task_text:
        return args.task_text.strip()
    return input("Задача> ").strip()


async def run_task(
    graph: Any,
    task: str,
    args: argparse.Namespace,
    config: dict[str, Any],
) -> Any:
    """Run one task on an already built graph."""

    if args.show_state:
        final_update: Any = None
        async for chunk in graph.astream(
            {"task": task},
            config=config,
            stream_mode="updates",
        ):
            final_update = chunk
            for node_name, update in chunk.items():
                print_step(node_name, update, args.json)

        if final_update is None:
            print("Agent finished without state updates.")
        return final_update

    result = await graph.ainvoke({"task": task}, config=config)
    _print_final_state(result, args.json)
    return result


def main() -> int:
    """Parse CLI arguments and run the async agent."""

    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(run_agent(args))


if __name__ == "__main__":
    raise SystemExit(main())
