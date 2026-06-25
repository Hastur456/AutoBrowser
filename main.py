import subprocess
import os
import socket
import asyncio
import argparse
from dotenv import load_dotenv
from langchain.messages import HumanMessage
from langchain_ollama import ChatOllama
from langchain_openrouter import ChatOpenRouter

from src.mcp.mcp_setup import setup_mcp
from src.agent import AgentWorkflow
from src.agent.state import AgentState

load_dotenv()

CHROME_PATH = os.getenv("CHROME_PATH")
USER_DATA_DIR = os.getenv("USER_DATA_DIR")
PORT = int(os.getenv("PORT"))

ALLOWED_TOOLS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_wait_for",
    "browser_tabs",
    "browser_navigate_back",
]


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AutoBrowser — LangGraph browser automation agent"
    )
    parser.add_argument(
        "--task", "-t",
        type=str,
        default=None,
        help="Task for the agent (e.g. 'Navigate to habr.com and click the first article')",
    )
    parser.add_argument(
        "--loop", "-l",
        action="store_true",
        help="Run in interactive loop mode (exit with 'quit' or Ctrl+C)",
    )
    return parser.parse_args()


async def run_task(agent: AgentWorkflow, task: str) -> dict:
    state = AgentState(
        messages=[HumanMessage(content=task)],
        error_count=0,
        retry_attempts=0,
        total_tool_calls=0,
        last_error_type=None,
        last_action=None,
        observation=None,
        perception=None,
        reflection=None,
        replan_count=0,
    )
    return await agent.run(state)


async def main():
    args = parse_args()

    subprocess.Popen([
        CHROME_PATH,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--remote-allow-origins=*",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ])

    while not is_port_open(PORT):
        print("Подключение к серверу...")
        await asyncio.sleep(0.5)

    print("=== Сервер подключен ===")

    tools = await setup_mcp()
    tools_surface = [tool for tool in tools if tool.name in ALLOWED_TOOLS]

    llm = ChatOpenRouter(
        model="deepseek/deepseek-v4-flash",
        temperature=0,
        disable_streaming=False,
    )

    agent = AgentWorkflow(llm=llm, tools=tools_surface, max_retries=3)

    if args.loop:
        print("Интерактивный режим. Введите 'quit' для выхода.\n")
        while True:
            try:
                task = input("Задача> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nВыход.")
                break
            if not task:
                continue
            if task.lower() in ("quit", "exit", "выход"):
                break
            result = await run_task(agent, task)
            print(result)
            print()
    else:
        task = args.task
        if not task:
            task = input("Задача> ").strip()
        result = await run_task(agent, task)
        print(result)
        return result


if __name__ == "__main__":
    asyncio.run(main())
