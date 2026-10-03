"""Long-lived session runtime for process-scoped application resources."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable, Iterator, Mapping, MutableMapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.agent_loop.engine import HumanInputCallback, native_task_runner
from src.agent_loop.events import (
    AgentTraceSink,
    CompositeEventSink,
    EventEmitter,
    JsonlEventSink,
)
from src.agent_loop.execution.completion import native_latest_state_loader
from src.agent_loop.execution.approval import ApprovalJudge
from src.agent_loop.execution.progress import render_action_history
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.goals import GoalRunRequest, GoalRunner
from src.config import get_settings
from src.contracts import PermissionMode
from src.harness.hooks import HookEngine, NullHookEngine, registry_digest
from src.browser.memory import BrowserMemoryPolicy, BrowserMemoryScope
from src.browser.permissions import BrowserResourceResolver
from src.harness.permissions import PermissionEngine
from src.harness.mcp_setup import MCPRuntime, build_mcp_runtime
from src.harness.memory import MemoryManager, NullMemoryContext
from src.harness.memory_consolidation import MemoryConsolidator
from src.harness.memory_store import MemoryContext, MemoryStore
from src.harness.memory_tool import memory_tools
from src.harness.runtime import (
    HARNESS_EVENT_METADATA_CONFIG_KEY,
    HARNESS_STATE_OVERRIDES_CONFIG_KEY,
    BrowserHarness,
)
from src.harness.telemetry import TelemetryObserver
from src.harness.tools import ToolRegistry


LLMFactory = Callable[..., Any]
HarnessFactory = Callable[..., BrowserHarness]
MCPRuntimeFactory = Callable[[int], MCPRuntime]
EventHandler = Callable[[str, object | None], None]

EXIT_COMMANDS = {"quit", "exit"}

SERVER_CONNECTED_MESSAGE = "=== Server connected ==="

INTERACTIVE_MESSAGE = "Interactive mode. Type 'quit' or 'exit' to quit.\n"

TASK_PROMPT = "Task> "

EXIT_MESSAGE = "\nExiting."
SESSION_THREAD_PREFIX = "session-"

SESSION_STATE_KEYS = (
    "messages",
    "observation",
    "snapshot",
    "browser",
    "last_tool",
    "last_args",
    "last_tool_request",
    "last_browser_action",
    "ineffective_browser_action",
    "ineffective_browser_actions",
    "pending_browser_tab_index",
    "pending_browser_tab_reason",
)

TASK_BOUNDARY_RESETS: dict[str, Any] = {
    "plan": [],
    "current_step": 0,
    "decision": "",
    "tool_request": {},
    "tool_result": {},
    "policy_decision": "",
    "final_answer": "",
    "error": "",
    "repeat_count": 0,
    "replan_count": 0,
    "consecutive_failures": 0,
    "ineffective_action_count": 0,
    "counters": {},
    "policy_event": {},
    "working_notes": "",
}


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
    agent_loop: bool
    chrome_path: str
    user_data_dir: str
    cdp_port: int
    cdp_timeout: float
    turn_cap: int
    #: Overrides ``settings.permissions.mode`` for this session; ``None`` keeps it.
    permission_mode: PermissionMode | None = None

    @classmethod
    def from_args(cls, args: Any) -> "SessionConfig":
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
            agent_loop=args.agent_loop,
            chrome_path=args.chrome_path,
            user_data_dir=args.user_data_dir,
            cdp_port=args.cdp_port,
            cdp_timeout=args.cdp_timeout,
            turn_cap=args.turn_cap,
            permission_mode=getattr(args, "permission_mode", None),
        )

    def task_config(self) -> dict[str, Any]:
        """Return the task-run configuration shared by tasks in this session."""

        return {
            "turn_cap": self.turn_cap,
            "run_name": "AutoBrowser CLI task",
            "metadata": {
                "model": self.model,
                "temperature": self.temperature,
                "show_state": self.show_state,
                "hide_snapshot": self.hide_snapshot,
                "compress_tools": self.compress_tools,
                "agent_loop": self.agent_loop,
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

    def replace(self, values: MutableMapping[str, object]) -> None:
        """Replace all session-scoped values."""

        self._values = dict(values)


@dataclass
class TaskRecord:
    """One user task executed inside a session."""

    task: str
    started_at: datetime
    task_id: str = field(default_factory=lambda: f"task-{uuid4().hex}")
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


def default_mcp_runtime_factory(cdp_port: int) -> MCPRuntime:
    """Build the session's MCP runtime from settings (``mcp_servers``)."""

    return build_mcp_runtime(cdp_port=cdp_port)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseException):
        return {
            "type": type(value).__name__,
            "message": str(value),
        }
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _build_session_event_sink(session_dir: Path) -> CompositeEventSink:
    """Build the session's durable event sinks and projections."""

    return CompositeEventSink(
        [
            JsonlEventSink(session_dir / "events.jsonl"),
            AgentTraceSink(session_dir / "agent_trace.jsonl"),
        ]
    )


def _task_state_overrides(
    session_state: MutableMapping[str, object],
    *,
    task_id: str,
) -> dict[str, object]:
    carried_state = {
        key: session_state[key]
        for key in SESSION_STATE_KEYS
        if key in session_state
    }
    if carried_state.get("messages"):
        # The session owns the carry-forward, MemoryManager the shape of the history:
        # finished tasks past memory.keep_recent_tasks become one digest message each.
        carried_state["messages"] = MemoryManager(settings=get_settings().memory).digest_tasks(
            list(carried_state["messages"])  # type: ignore[arg-type]
        )
    reset_state = {
        key: value.copy() if isinstance(value, (dict, list)) else value
        for key, value in TASK_BOUNDARY_RESETS.items()
    }
    return {
        **carried_state,
        **reset_state,
        "task_id": task_id,
    }


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
    session_dir: Path | None = None
    workspace: WorkspaceContext | None = None
    artifacts: ArtifactRegistry = field(default_factory=ArtifactRegistry)
    state: SessionState = field(default_factory=SessionState)
    tasks: list[TaskRecord] = field(default_factory=list)
    current_task: str | None = None
    metadata: SessionMetadata = field(default_factory=SessionMetadata)
    events: SessionEventBus = field(default_factory=SessionEventBus)
    harness: BrowserHarness | None = None
    llm: Any | None = None
    tool_registry: ToolRegistry | None = None
    mcp: MCPRuntime | None = None
    telemetry: TelemetryObserver = field(default_factory=TelemetryObserver)
    event_emitter: EventEmitter = field(default_factory=EventEmitter)
    #: Session-scoped lifecycle hooks, loaded from ``settings.hooks`` in :meth:`initialize`.
    hooks: HookEngine | NullHookEngine = field(default_factory=NullHookEngine)
    hooks_registry_sha256: str = ""
    #: Session-scoped tool authorization (rules, mode, approval grants), loaded from
    #: ``settings.permissions`` in :meth:`initialize`.
    permissions: PermissionEngine = field(default_factory=PermissionEngine)
    #: Session-scoped model judgments that escalate a call to approval, built from
    #: ``settings.permissions.approval_judge`` in :meth:`initialize`.
    approval: ApprovalJudge = field(default_factory=ApprovalJudge)
    #: Session-scoped persistent memory (``settings.memory.persistent_enabled``), built in
    #: :meth:`initialize`; ``None`` when switched off.
    memory_store: MemoryStore | None = None
    #: Renders the ``Memory`` context block for the engine (``EngineResources.memory``).
    memory: MemoryContext | NullMemoryContext = field(default_factory=NullMemoryContext)
    chrome_process: Any | None = None
    initialized: bool = False

    async def initialize(
        self,
        *,
        llm_factory: LLMFactory,
        start_chrome_cdp: Callable[[str, str, int], Any],
        wait_for_port: Callable[[int, float], Awaitable[None]],
        mcp_runtime_factory: MCPRuntimeFactory,
        output_fn: Callable[..., None],
        print_tools: Callable[[list[Any]], None] | None,
        harness_factory: HarnessFactory,
    ) -> None:
        """Initialize session-owned runtime resources once."""

        if self.initialized:
            return

        # Load hooks and permissions first: a broken registry or rule must fail startup
        # before Chrome/MCP launch.
        settings = get_settings()
        self.hooks = HookEngine.from_settings(
            settings.hooks,
            progress_timeout_seconds=settings.loop.progress_timeout_seconds,
        )
        self.hooks_registry_sha256 = registry_digest(settings.hooks)
        self.permissions = PermissionEngine.from_settings(
            settings.permissions,
            resolver=BrowserResourceResolver(),
            mode=self.config.permission_mode,
        )
        self._initialize_memory(settings)
        session_tools = (
            memory_tools(self.memory_store)
            if self.memory_store is not None and settings.memory.tool_enabled
            else []
        )

        now = datetime.now(UTC)
        self.metadata.started_at = now
        self.metadata.last_activity = now
        self.session_dir = get_settings().storage.session_dir(self.session_id).resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.event_emitter = EventEmitter(
            _build_session_event_sink(self.session_dir),
            session_id=self.session_id,
        )
        self.workspace = WorkspaceContext(
            self.session_dir / get_settings().storage.workspace_subdir
        )
        self.workspace.initialize()

        self.llm = llm_factory(
            model=self.config.model,
            temperature=self.config.temperature,
        )
        self.approval = ApprovalJudge.from_settings(
            settings.permissions,
            llm=self.llm,
            llm_factory=llm_factory,
        )
        if self.config.no_mcp:
            self.tool_registry = ToolRegistry(session_tools)
        else:
            mcp: MCPRuntime | None = None
            try:
                self.chrome_process = start_chrome_cdp(
                    self.config.chrome_path,
                    self.config.user_data_dir,
                    self.config.cdp_port,
                )
                await wait_for_port(self.config.cdp_port, self.config.cdp_timeout)
                output_fn(SERVER_CONNECTED_MESSAGE)
                mcp = mcp_runtime_factory(self.config.cdp_port)
                await mcp.start()  # raises if the browser server cannot start
            except BaseException:
                if mcp is not None:
                    with contextlib.suppress(Exception):
                        await mcp.close()
                self._close_chrome_process()
                raise
            self.mcp = mcp
            # The MCP tool source is live: rediscovery (list_changed) and servers going
            # down / coming back are reflected on the next registry read.
            self.tool_registry = ToolRegistry(
                session_tools,
                providers=[mcp.tool_source],
                normalizers=mcp.normalizers,
            )
            if self.config.show_tools and print_tools is not None:
                print_tools(list(await self.tool_registry.get_all()))

        self.harness = harness_factory(
            llm=self.llm,
            tool_registry=self.tool_registry,
            telemetry=self.telemetry,
            event_emitter=self.event_emitter,
            compress_tools=self.config.compress_tools,
        )
        self.initialized = True
        self.persist()
        self.event_emitter.emit(
            "session.started",
            source="harness.session",
            payload={"session_id": self.session_id},
        )
        self.events.emit("session.started", self)

    def _initialize_memory(self, settings: Any) -> None:
        """Build the persistent memory store and its context renderer (or switch them off)."""

        memory = settings.memory
        if not memory.persistent_enabled:
            self.memory_store = None
            self.memory = NullMemoryContext()
            return
        self.memory_store = MemoryStore.from_settings(
            memory,
            settings.storage,
            policy=BrowserMemoryPolicy(),
            on_event=self.emit_memory_event,
        )
        self.memory = MemoryContext(
            self.memory_store,
            BrowserMemoryScope(),
            tools_enabled=memory.tool_enabled,
        )

    def emit_memory_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Forward a memory event (``memory.*``) to the session's event stream."""

        task_id = self.memory_store.task_id if self.memory_store is not None else ""
        self.event_emitter.emit(
            event_type,  # type: ignore[arg-type]
            source="harness.memory",
            payload=payload,
            task_id=task_id or None,
            goal_id=task_id or None,
        )

    def reset_task(self, task: str, *, task_id: str | None = None) -> TaskRecord:
        """Start tracking a new task inside the session."""

        now = datetime.now(UTC)
        record = TaskRecord(task=task, started_at=now, task_id=task_id or f"task-{uuid4().hex}")
        self.current_task = task
        self.tasks.append(record)
        if self.memory_store is not None:
            self.memory_store.bind_task(record.task_id)
        self.metadata.last_activity = now
        self.persist()
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
        self.persist()
        self.events.emit("task.finished", record)

    def fail_task(self, record: TaskRecord, exception: BaseException) -> None:
        """Mark a tracked task as failed while preserving the exception."""

        now = datetime.now(UTC)
        record.result = exception
        record.finished_at = now
        self.current_task = None
        self.metadata.task_count += 1
        self.metadata.last_activity = now
        self.persist()
        self.events.emit("task.failed", record)

    def snapshot(self) -> dict[str, Any]:
        """Return a durable, JSON-compatible view of the session."""

        workspace = self.workspace
        return {
            "session_id": self.session_id,
            "initialized": self.initialized,
            "current_task": self.current_task,
            "config": asdict(self.config),
            "metadata": asdict(self.metadata),
            "workspace": {
                "root": workspace.root if workspace else None,
                "downloads": workspace.downloads if workspace else None,
                "screenshots": workspace.screenshots if workspace else None,
                "temp": workspace.temp if workspace else None,
                "artifacts": workspace.artifacts if workspace else None,
            },
            "artifacts": [asdict(artifact) for artifact in self.artifacts.all()],
            "tasks": [asdict(task) for task in self.tasks],
            "hooks": {
                "enabled": isinstance(self.hooks, HookEngine),
                "registry_sha256": self.hooks_registry_sha256,
            },
            "permissions": {
                "mode": self.permissions.mode,
                "approval_judge": self.approval.mode,
            },
            "memory": self._memory_snapshot(),
        }

    def _memory_snapshot(self) -> dict[str, Any]:
        store = self.memory_store
        if store is None:
            return {"enabled": False, "root": "", "entries": 0, "tools": False}
        return {
            "enabled": True,
            "root": str(store.root),
            "entries": len(store.entries()),
            "tools": isinstance(self.memory, MemoryContext) and self.memory.tools_enabled,
        }

    def persist(self) -> None:
        """Persist session metadata and task records under .autobrowser."""

        if self.session_dir is None:
            self.session_dir = get_settings().storage.session_dir(
                self.session_id
            ).resolve()
            self.session_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot()
        _write_json(self.session_dir / "session.json", snapshot)
        _write_json(self.session_dir / "tasks.json", snapshot["tasks"])

    async def close(self) -> None:
        """Release session-owned runtime resources.

        MCP servers are shut down first (Playwright detaches from CDP), then Chrome.
        """

        mcp = self.mcp
        self.mcp = None
        if mcp is not None:
            await mcp.close()
        self._close_chrome_process()
        self.event_emitter.emit(
            "session.closed",
            source="harness.session",
            payload={"session_id": self.session_id},
        )
        self.harness = None
        self.llm = None
        self.tool_registry = None
        self.current_task = None
        self.metadata.last_activity = datetime.now(UTC)
        self.initialized = False
        self.persist()
        self.events.emit("session.closed", self)

    def _close_chrome_process(self) -> None:
        process = self.chrome_process
        self.chrome_process = None
        if process is None:
            return
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            return
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            terminate()
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                wait(timeout=5)
            except TypeError:
                wait()
            except Exception:
                kill = getattr(process, "kill", None)
                if callable(kill):
                    kill()


class SessionRuntime:
    """Own process-lifetime resources and delegate user tasks to the agent."""

    def __init__(
        self,
        config: SessionConfig,
        *,
        llm_factory: LLMFactory,
        start_chrome_cdp: Callable[[str, str, int], Any],
        wait_for_port: Callable[[int, float], Awaitable[None]],
        mcp_runtime_factory: MCPRuntimeFactory = default_mcp_runtime_factory,
        print_tools: Callable[[list[Any]], None] | None = None,
        harness_factory: HarnessFactory = BrowserHarness,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[..., None] = print,
        human_input: HumanInputCallback | None = None,
    ) -> None:
        self.config = config
        self.context = SessionContext(config)
        self._llm_factory = llm_factory
        self._start_chrome_cdp = start_chrome_cdp
        self._wait_for_port = wait_for_port
        self._mcp_runtime_factory = mcp_runtime_factory
        self._print_tools = print_tools
        self._harness_factory = harness_factory
        self._input = input_fn
        self._output = output_fn
        #: Answers permission ``ask`` verdicts; ``None`` denies them (headless runs).
        self._human_input = human_input

    @property
    def harness(self) -> BrowserHarness:
        """Return the initialized harness."""

        if self.context.harness is None:
            raise RuntimeError("SessionRuntime has not been started.")
        return self.context.harness

    async def start(self) -> None:
        """Initialize long-lived resources once for this process session."""

        if self.context.initialized:
            return
        await self.context.initialize(
            llm_factory=self._llm_factory,
            start_chrome_cdp=self._start_chrome_cdp,
            wait_for_port=self._wait_for_port,
            mcp_runtime_factory=self._mcp_runtime_factory,
            output_fn=self._output,
            print_tools=self._print_tools,
            harness_factory=self._harness_factory,
        )
        if self.context.permissions.mode == "bypass":
            self._output(
                "WARNING: permission mode 'bypass' grants every approval; only deny rules "
                "and always-ask rules still hold."
            )

    async def run_task(self, task: str) -> Any:
        """Run one user task through the existing agent implementation."""

        await self.start()
        task_id = f"task-{uuid4().hex}"
        record = self.context.reset_task(task, task_id=task_id)
        task_config = self.config.task_config()
        configurable = dict(task_config.get("configurable") or {})
        configurable["thread_id"] = self._session_thread_id()
        task_config["configurable"] = configurable
        event_metadata = dict(task_config.get(HARNESS_EVENT_METADATA_CONFIG_KEY) or {})
        event_metadata.update(
            {
                "session_id": self.context.session_id,
                "task_id": task_id,
                "goal_id": task_id,
            }
        )
        task_config[HARNESS_EVENT_METADATA_CONFIG_KEY] = event_metadata
        task_config[HARNESS_STATE_OVERRIDES_CONFIG_KEY] = _task_state_overrides(
            self.context.state,
            task_id=task_id,
        )
        request = GoalRunRequest(
            task=task,
            task_id=task_id,
            goal_id=task_id,
            thread_id=self._session_thread_id(),
            config=task_config,
            state_overrides=task_config[HARNESS_STATE_OVERRIDES_CONFIG_KEY],
        )
        latest_state: dict[str, object] | None = None

        async def load_latest_state(
            config: Mapping[str, Any],
            fallback: Any | None,
        ) -> dict[str, object] | None:
            nonlocal latest_state
            latest_state = await native_latest_state_loader(config, fallback)
            return latest_state

        resources = EngineResources.from_harness(
            self.harness,
            llm=self.context.llm,
            events=self.context.event_emitter,
            hooks=self.context.hooks,
            permissions=self.context.permissions,
            approval=self.context.approval,
            memory=self.context.memory,
        )
        runner = GoalRunner(
            harness=self.harness,
            session_config=self.config,
            task_runner=native_task_runner(resources, human_input=self._human_input),
            event_emitter=self.context.event_emitter,
            latest_state_loader=load_latest_state,
        )
        try:
            goal_result = await runner.run(request)
        except Exception as exc:
            if latest_state is not None:
                self.context.state.replace(latest_state)
            self._forget_memory(task_id)
            self.context.fail_task(record, exc)
            raise
        if goal_result.latest_state is not None:
            self.context.state.replace(dict(goal_result.latest_state))
        await self._update_memory(task, task_id, goal_result.status, goal_result.result)
        self.context.finish_task(record, goal_result.result)
        return goal_result.result

    async def _update_memory(self, task: str, task_id: str, status: str, result: Any) -> None:
        """After ``goal_end``: staged trust for the entries the task loaded, then the opt-in
        consolidation of a completed task. Memory never fails a task."""

        store = self.context.memory_store
        if store is None:
            return
        outcome = {"completed": "done", "blocked": "blocked"}.get(status, "cancelled")
        try:
            store.record_outcome(task_id, outcome)
        except OSError as exc:
            reason = f"outcome: {exc}"[: store.settings.event_reason_chars]
            self.context.emit_memory_event(
                "memory.skipped", {"task_id": task_id, "reason": reason}
            )
        settings = get_settings().memory
        if outcome == "done" and settings.consolidate_on_goal_end and self.context.llm is not None:
            state = getattr(result, "state", None)
            history = list(getattr(state, "action_history", None) or [])
            domains: set[str] = set()
            if isinstance(self.context.memory, MemoryContext):
                domains = set(self.context.memory.visited(task_id))
            if state is not None:
                domains.add(BrowserMemoryScope().scope(state.snapshot_mapping()))
            consolidator = MemoryConsolidator(
                self.context.llm,
                store,
                timeout_seconds=settings.consolidation_timeout_seconds,
                on_event=self.context.emit_memory_event,
            )
            await consolidator.consolidate(
                task_id=task_id,
                task=task,
                final_answer=str(getattr(result, "final_answer", "") or ""),
                action_history=render_action_history(history, len(history)),
                domains=domains,
            )
        self._forget_memory(task_id)

    def _forget_memory(self, task_id: str) -> None:
        store = self.context.memory_store
        if store is not None:
            store.record_outcome(task_id, "cancelled")  # drops what the task loaded
        if isinstance(self.context.memory, MemoryContext):
            self.context.memory.forget(task_id)

    def _session_thread_id(self) -> str:
        return f"{SESSION_THREAD_PREFIX}{self.context.session_id}"

    async def run_forever(self, *, initial_task: str | None = None) -> int:
        """Run tasks sequentially until the user exits the session."""

        await self.start()
        task = (initial_task or "").strip()
        if task:
            self._print_result(await self.run_task(task))
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

            self._print_result(await self.run_task(task))
            self._output()

    def _print_result(self, result: Any) -> None:
        """Print a task's final answer to the interactive loop output."""

        answer = getattr(result, "final_answer", None)
        if not answer and isinstance(result, Mapping):
            answer = result.get("final_answer")
        if answer:
            self._output(answer)

    async def close(self) -> None:
        """Release process-lifetime external resources."""

        await self.context.close()


__all__ = [
    "Artifact",
    "ArtifactRegistry",
    "EXIT_COMMANDS",
    "MCPRuntimeFactory",
    "SessionConfig",
    "SessionContext",
    "SessionEventBus",
    "SessionMetadata",
    "SessionRuntime",
    "SessionState",
    "SESSION_THREAD_PREFIX",
    "TaskRecord",
    "WorkspaceContext",
    "default_mcp_runtime_factory",
]
