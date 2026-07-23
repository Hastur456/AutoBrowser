"""Long-lived session runtime for process-scoped application resources."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.harness.memory import MemoryManager
from src.harness.runtime import BrowserHarness, GraphBuilder
from src.harness.telemetry import TelemetryObserver
from src.harness.tools import ToolRegistry


TaskRunner = Callable[[BrowserHarness, str, Any, dict[str, Any]], Awaitable[Any]]
LLMFactory = Callable[..., Any]
HarnessFactory = Callable[..., BrowserHarness]
EventHandler = Callable[[str, object | None], None]

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


class SessionState(MutableMapping[str, object]):
    """Mutable session state with a replaceable backing store."""

    def __init__(self, initial: MutableMapping[str, object] | None = None) -> None:
        self._values: dict[str, object] = dict(initial or {})

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def set(self, key: str, value: object) -> None:
        """Set a session-scoped value."""

        self._values[key] = value


@dataclass
class TaskRecord:
    """One user task executed inside a session."""

    task: str
    started_at: datetime
    finished_at: datetime | None = None
    result: Any | None = None


@dataclass
class SessionMetadata:
    """Session-owned metadata, separate from user configuration."""

    started_at: datetime | None = None
    last_activity: datetime | None = None
    task_count: int = 0
    runtime_version: str | None = None


@dataclass(frozen=True)
class Artifact:
    """A file or durable output produced during a session."""

    name: str
    path: Path
    kind: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ArtifactRegistry:
    """Track artifacts produced by tools and session code."""

    def __init__(self) -> None:
        self._artifacts: list[Artifact] = []

    def register(
        self,
        name: str,
        path: Path,
        *,
        kind: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        """Register an artifact without taking ownership of file contents."""

        artifact = Artifact(
            name=name,
            path=Path(path),
            kind=kind,
            metadata=dict(metadata or {}),
        )
        self._artifacts.append(artifact)
        return artifact

    def latest(self, kind: str | None = None) -> Artifact | None:
        """Return the most recently registered artifact, optionally by kind."""

        for artifact in reversed(self._artifacts):
            if kind is None or artifact.kind == kind:
                return artifact
        return None

    def all(self) -> list[Artifact]:
        """Return registered artifacts in insertion order."""

        return list(self._artifacts)


@dataclass
class WorkspaceContext:
    """Filesystem workspace for one session."""

    root: Path
    downloads: Path = field(init=False)
    screenshots: Path = field(init=False)
    temp: Path = field(init=False)
    artifacts: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.downloads = self.root / "downloads"
        self.screenshots = self.root / "screenshots"
        self.temp = self.root / "temp"
        self.artifacts = self.root / "artifacts"

    def initialize(self) -> None:
        """Create the workspace directory layout."""

        for path in (
            self.root,
            self.downloads,
            self.screenshots,
            self.temp,
            self.artifacts,
        ):
            path.mkdir(parents=True, exist_ok=True)


class SessionEventBus:
    """Synchronous event bus for session lifecycle events."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        """Register a handler for a named event."""

        self._subscribers.setdefault(event_name, []).append(handler)

    def emit(self, event_name: str, payload: object | None = None) -> None:
        """Emit an event to current subscribers."""

        for handler in self._subscribers.get(event_name, []):
            handler(event_name, payload)


@dataclass
class SessionContext:
    """Root object for one process-scoped AutoBrowser session."""

    config: SessionConfig
    session_id: str = field(default_factory=lambda: uuid4().hex)
    workspace: WorkspaceContext | None = None
    artifacts: ArtifactRegistry = field(default_factory=ArtifactRegistry)
    state: SessionState = field(default_factory=SessionState)
    tasks: list[TaskRecord] = field(default_factory=list)
    current_task: str | None = None
    metadata: SessionMetadata = field(default_factory=SessionMetadata)
    events: SessionEventBus = field(default_factory=SessionEventBus)
    harness: BrowserHarness | None = None
    llm: Any | None = None
    memory: MemoryManager | None = None
    tool_registry: ToolRegistry | None = None
    telemetry: TelemetryObserver = field(default_factory=TelemetryObserver)
    initialized: bool = False

    async def initialize(
        self,
        *,
        graph_builder: GraphBuilder,
        llm_factory: LLMFactory,
        start_chrome_cdp: Callable[[str, str, int], Any],
        wait_for_port: Callable[[int, float], Awaitable[None]],
        load_browser_tools: Callable[[int], Awaitable[Sequence[Any]]],
        output_fn: Callable[..., None],
        print_tools: Callable[[list[Any]], None] | None,
        harness_factory: HarnessFactory,
    ) -> None:
        """Initialize session-owned runtime resources once."""

        if self.initialized:
            return

        now = datetime.now(UTC)
        self.metadata.started_at = now
        self.metadata.last_activity = now
        self.workspace = WorkspaceContext(
            Path(".autobrowser") / "sessions" / self.session_id / "workspace"
        )
        self.workspace.initialize()

        self.llm = llm_factory(
            model=self.config.model,
            temperature=self.config.temperature,
        )
        tools: list[Any]
        if self.config.no_mcp:
            tools = []
        else:
            start_chrome_cdp(
                self.config.chrome_path,
                self.config.user_data_dir,
                self.config.cdp_port,
            )
            await wait_for_port(self.config.cdp_port, self.config.cdp_timeout)
            output_fn(SERVER_CONNECTED_MESSAGE)
            tools = list(await load_browser_tools(self.config.cdp_port))
            if self.config.show_tools and print_tools is not None:
                print_tools(tools)

        self.memory = MemoryManager()
        self.tool_registry = ToolRegistry(tools=tools)
        self.harness = harness_factory(
            graph_builder,
            llm=self.llm,
            tool_registry=self.tool_registry,
            memory_manager=self.memory,
            telemetry=self.telemetry,
            compress_tools=self.config.compress_tools,
        )
        self.initialized = True
        self.events.emit("session.started", self)

    def reset_task(self, task: str) -> TaskRecord:
        """Start tracking a new task inside the session."""

        now = datetime.now(UTC)
        record = TaskRecord(task=task, started_at=now)
        self.current_task = task
        self.tasks.append(record)
        self.metadata.last_activity = now
        self.events.emit("task.started", record)
        return record

    def finish_task(self, record: TaskRecord, result: Any) -> None:
        """Mark a tracked task as completed."""

        now = datetime.now(UTC)
        record.result = result
        record.finished_at = now
        self.current_task = None
        self.metadata.task_count += 1
        self.metadata.last_activity = now
        self.events.emit("task.finished", record)

    def fail_task(self, record: TaskRecord, exception: BaseException) -> None:
        """Mark a tracked task as failed while preserving the exception."""

        now = datetime.now(UTC)
        record.result = exception
        record.finished_at = now
        self.current_task = None
        self.metadata.task_count += 1
        self.metadata.last_activity = now
        self.events.emit("task.failed", record)

    async def close(
        self,
        close_external: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Release session-owned runtime resources."""

        if close_external is not None:
            await close_external()
        self.harness = None
        self.llm = None
        self.memory = None
        self.tool_registry = None
        self.current_task = None
        self.metadata.last_activity = datetime.now(UTC)
        self.initialized = False
        self.events.emit("session.closed", self)


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
        self.context = SessionContext(config)
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

    @property
    def harness(self) -> BrowserHarness:
        """Return the initialized harness."""

        if self.context.harness is None:
            raise RuntimeError("SessionRuntime has not been started.")
        return self.context.harness

    async def start(self) -> None:
        """Initialize long-lived resources once for this process session."""

        await self.context.initialize(
            graph_builder=self._graph_builder,
            llm_factory=self._llm_factory,
            start_chrome_cdp=self._start_chrome_cdp,
            wait_for_port=self._wait_for_port,
            load_browser_tools=self._load_browser_tools,
            output_fn=self._output,
            print_tools=self._print_tools,
            harness_factory=self._harness_factory,
        )

    async def run_task(self, task: str) -> Any:
        """Run one user task through the existing agent implementation."""

        await self.start()
        record = self.context.reset_task(task)
        try:
            result = await self._task_runner(
                self.harness,
                task,
                self.config,
                self.config.task_config(),
            )
        except Exception as exc:
            self.context.fail_task(record, exc)
            raise
        self.context.finish_task(record, result)
        return result

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

        close_external = None if self.config.no_mcp else self._close_mcp_session
        await self.context.close(close_external)


__all__ = [
    "Artifact",
    "ArtifactRegistry",
    "EXIT_COMMANDS",
    "SessionConfig",
    "SessionContext",
    "SessionEventBus",
    "SessionMetadata",
    "SessionRuntime",
    "SessionState",
    "TaskRecord",
    "WorkspaceContext",
]
