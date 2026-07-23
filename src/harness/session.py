"""Long-lived session runtime for process-scoped application resources."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from src.harness.runtime import BrowserHarness, GraphBuilder


TaskRunner = Callable[[BrowserHarness, str, Any, dict[str, Any]], Awaitable[Any]]
LLMFactory = Callable[..., Any]
HarnessFactory = Callable[..., BrowserHarness]

EXIT_COMMANDS = {"quit", "exit"}

SERVER_CONNECTED_MESSAGE = "=== Server connected ==="

INTERACTIVE_MESSAGE = "Interactive mode. Type 'quit' or 'exit' to quit.\n"

TASK_PROMPT = "Task> "

EXIT_MESSAGE = "\nExiting."


@dataclass(frozen=True)
class SessionConfig:
    """Configuration used to build and run a process-long AutoBrowser session."""

    model: str
    temperature: float
    no_mcp: bool
    show_state: bool
    hide_snapshot: bool
    show_tools: bool
    as_json: bool
    compress_tools: bool
    chrome_path: str
    user_data_dir: str
    cdp_port: int
    cdp_timeout: float
    recursion_limit: int
    tracing_enabled: bool

    @classmethod
    def from_args(cls, args: Any, *, tracing_enabled: bool) -> "SessionConfig":
        """Build session configuration from parsed CLI args."""

        return cls(
            model=args.model,
            temperature=args.temperature,
            no_mcp=args.no_mcp,
            show_state=args.show_state,
            hide_snapshot=args.hide_snapshot,
            show_tools=args.show_tools,
            as_json=args.json,
            compress_tools=args.compress_tools,
            chrome_path=args.chrome_path,
            user_data_dir=args.user_data_dir,
            cdp_port=args.cdp_port,
            cdp_timeout=args.cdp_timeout,
            recursion_limit=args.recursion_limit,
            tracing_enabled=tracing_enabled,
        )

    def task_config(self) -> dict[str, Any]:
        """Return the LangGraph config shared by tasks in this session."""

        return {
            "recursion_limit": self.recursion_limit,
            "run_name": "AutoBrowser CLI task",
            "metadata": {
                "model": self.model,
                "temperature": self.temperature,
                "show_state": self.show_state,
                "hide_snapshot": self.hide_snapshot,
                "langsmith_tracing": self.tracing_enabled,
                "compress_tools": self.compress_tools,
            },
            "tags": [
                "autobrowser",
                "cli",
            ],
        }


class SessionRuntime:
    """Own process-lifetime resources and delegate user tasks to the agent."""

    def __init__(
        self,
        config: SessionConfig,
        *,
        graph_builder: GraphBuilder,
        llm_factory: LLMFactory,
        task_runner: TaskRunner,
        start_chrome_cdp: Callable[[str, str, int], Any],
        wait_for_port: Callable[[int, float], Awaitable[None]],
        load_browser_tools: Callable[[int], Awaitable[Sequence[Any]]],
        close_mcp_session: Callable[[], Awaitable[None]],
        print_tools: Callable[[list[Any]], None] | None = None,
        harness_factory: HarnessFactory = BrowserHarness,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[..., None] = print,
    ) -> None:
        self.config = config
        self._graph_builder = graph_builder
        self._llm_factory = llm_factory
        self._task_runner = task_runner
        self._start_chrome_cdp = start_chrome_cdp
        self._wait_for_port = wait_for_port
        self._load_browser_tools = load_browser_tools
        self._close_mcp_session = close_mcp_session
        self._print_tools = print_tools
        self._harness_factory = harness_factory
        self._input = input_fn
        self._output = output_fn
        self._harness: BrowserHarness | None = None
        self._task_config: dict[str, Any] | None = None
        self._started = False

    @property
    def harness(self) -> BrowserHarness:
        """Return the initialized harness."""

        if self._harness is None:
            raise RuntimeError("SessionRuntime has not been started.")
        return self._harness

    async def start(self) -> None:
        """Initialize long-lived resources once for this process session."""

        if self._started:
            return

        llm = self._llm_factory(
            model=self.config.model,
            temperature=self.config.temperature,
        )
        tools: list[Any]
        if self.config.no_mcp:
            tools = []
        else:
            self._start_chrome_cdp(
                self.config.chrome_path,
                self.config.user_data_dir,
                self.config.cdp_port,
            )
            await self._wait_for_port(self.config.cdp_port, self.config.cdp_timeout)
            self._output(SERVER_CONNECTED_MESSAGE)
            tools = list(await self._load_browser_tools(self.config.cdp_port))
            if self.config.show_tools and self._print_tools is not None:
                self._print_tools(tools)

        self._harness = self._harness_factory(
            self._graph_builder,
            llm=llm,
            tools=tools,
            compress_tools=self.config.compress_tools,
        )
        self._task_config = self.config.task_config()
        self._started = True

    async def run_task(self, task: str) -> Any:
        """Run one user task through the existing agent implementation."""

        await self.start()
        return await self._task_runner(
            self.harness,
            task,
            self.config,
            dict(self._task_config or {}),
        )

    async def run_forever(self, *, initial_task: str | None = None) -> int:
        """Run tasks sequentially until the user exits the session."""

        await self.start()
        task = (initial_task or "").strip()
        if task:
            await self.run_task(task)
            self._output()

        self._output(INTERACTIVE_MESSAGE)
        while True:
            try:
                task = self._input(TASK_PROMPT).strip()
            except (KeyboardInterrupt, EOFError):
                self._output(EXIT_MESSAGE)
                return 0

            if not task:
                continue
            if task.lower() in EXIT_COMMANDS:
                return 0

            await self.run_task(task)
            self._output()

    async def close(self) -> None:
        """Release process-lifetime external resources."""

        if not self.config.no_mcp:
            await self._close_mcp_session()
        self._harness = None
        self._task_config = None
        self._started = False


__all__ = ["EXIT_COMMANDS", "SessionConfig", "SessionRuntime"]
