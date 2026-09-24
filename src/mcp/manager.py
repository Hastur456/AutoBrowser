"""Universal MCP Manager: registry-driven pool of MCP clients for the agent harness (Host).

Responsibilities (and nothing else): server registry, one client per server, lifecycle
(connect / initialize / reconnect / graceful shutdown), discovery of tools / resources /
resource templates / prompts with notification-driven invalidation, a single aggregated
catalog with collision-free names, and routing of calls to the right server.

Out of scope on purpose: permissions, hooks/middleware, observability, result caching,
provider-specific schema mapping. The typed errors in :mod:`src.mcp.errors` and the public
methods below are the extension points for those layers.

Key invariants (see the design doc, §3.4–§3.6):

* **Owner task per connection incarnation.** The SDK transports and ``ClientSession`` are
  anyio task groups / cancel scopes, which must be exited in the task that entered them.
  One long-lived ``_owner`` task enters the transport and the session, initializes,
  discovers, signals readiness and then *waits*. Stopping = waking it up (or cancelling it)
  so that it leaves the contexts itself. Requests may be sent from any task.
* **Transport EOF is observed.** A small relay between the transport's read stream and the
  session notices when the server goes away (stdio process exit) even when no request is
  in flight, instead of waiting for the next call to hang or fail.
* **``tools/call`` is never retried automatically.** Whether the server performed the action
  is unknown after a drop/timeout, and a stateful server (Playwright) comes back as a new,
  empty instance. The caller gets :class:`ServerConnectionLostError` with ``generation``
  and ``stateful`` and decides. Idempotent requests (read_resource, get_prompt) are retried
  once after recovery.
* **Every request has a timeout** (``read_timeout_seconds`` of the SDK), and in-flight
  requests are drained/cancelled on close, so callers never wait forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypeVar

import anyio
from mcp import ClientSession, McpError, types
from pydantic import AnyUrl

from src.mcp.catalog import (
    ALL_KINDS,
    Catalog,
    ConnectionState,
    PromptDescriptor,
    ResourceDescriptor,
    ResourceTemplateDescriptor,
    ServerStatus,
    ToolDescriptor,
    discover,
)
from src.mcp.config import ServerRegistry, parse_server_config
from src.mcp.errors import (
    AmbiguousResourceError,
    RequestTimeoutError,
    ServerClosedError,
    ServerConnectionLostError,
    ServerStateLostError,
    ServerUnavailableError,
    UnknownPromptError,
    UnknownResourceError,
    UnknownServerError,
    UnknownToolError,
)
from src.mcp.naming import MAX_TOOL_NAME, qualify, validate_server_name
from src.mcp.transports import open_transport

logger = logging.getLogger(__name__)

T = TypeVar("T")
SessionOp = Callable[[ClientSession], Coroutine[Any, Any, T]]

# Verified against mcp 1.27: a missing response raises McpError(code=httpx 408), a dropped
# receive loop answers every pending request with McpError(CONNECTION_CLOSED=-32000,
# "Connection closed"); writes to a dead transport raise anyio stream errors.
REQUEST_TIMEOUT_CODE = 408
_LOST_GRACE_S = 1.0
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
)

_LIST_CHANGED: dict[type, str] = {
    types.ToolListChangedNotification: "tools",
    types.ResourceListChangedNotification: "resources",
    types.PromptListChangedNotification: "prompts",
}


# --------------------------------------------------------------------------- helpers


def _leaf(exc: BaseException) -> BaseException:
    """Unwrap anyio/SDK exception groups down to the most informative leaf."""

    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        non_cancel = [e for e in exc.exceptions if not isinstance(e, asyncio.CancelledError)]
        exc = (non_cancel or list(exc.exceptions))[0]
    return exc


def _describe(exc: BaseException) -> str:
    leaf = _leaf(exc)
    text = str(leaf)
    return f"{type(leaf).__name__}: {text}" if text else type(leaf).__name__


def _is_timeout(exc: McpError) -> bool:
    return exc.error.code == REQUEST_TIMEOUT_CODE


def _cancelling() -> bool:
    task = asyncio.current_task()
    return bool(task is not None and task.cancelling())


# --------------------------------------------------------------------------- runtime state


@dataclass(eq=False)
class ManagedServer:
    """Runtime state of one server (one per registry entry)."""

    name: str
    config: Any  # StdioServerConfig | StreamableHttpServerConfig
    state: ConnectionState = ConnectionState.DISCONNECTED
    session: ClientSession | None = None
    server_info: types.Implementation | None = None
    capabilities: types.ServerCapabilities | None = None

    # catalog: the last successfully fetched one; survives a dropped connection
    tools: dict[str, ToolDescriptor] = field(default_factory=dict)
    resources: dict[str, ResourceDescriptor] = field(default_factory=dict)
    resource_templates: list[ResourceTemplateDescriptor] = field(default_factory=list)
    prompts: dict[str, PromptDescriptor] = field(default_factory=dict)
    catalog_stale: bool = False
    dirty: set[str] = field(default_factory=set)  # kinds to rediscover after list_changed

    last_error: str | None = None
    reconnect_attempts: int = 0
    # Incremented on every successful (re)connect. For stateful servers a new generation
    # means the server-side state (browser, pages, cookies) is gone.
    generation: int = 0

    # --- connection ownership ---
    owner_task: asyncio.Task[Any] | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    stop_requested: bool = False  # a requested stop of the current incarnation
    closing: bool = False  # shutdown/remove/manual reconnect: do not auto-reconnect
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reconnect_task: asyncio.Task[Any] | None = None
    rediscover_task: asyncio.Task[Any] | None = None
    liveness_task: asyncio.Task[Any] | None = None
    inflight: set[asyncio.Task[Any]] = field(default_factory=set)

    @property
    def ephemeral(self) -> bool:
        return getattr(self.config, "connection_mode", "persistent") == "ephemeral"


# --------------------------------------------------------------------------- manager


class MCPManager:
    """Host-side pool of MCP clients with an aggregated, routable catalog.

    Usage::

        async with MCPManager(ServerRegistry.from_mapping(settings.mcp_servers)) as mcp:
            tools = mcp.list_tools()
            result = await mcp.call_tool("playwright__browser_click", {...})

    ``start()`` and ``shutdown()`` may be called from different tasks — connection contexts
    are always entered and exited inside their own owner tasks.
    """

    def __init__(
        self,
        registry: ServerRegistry | Mapping[str, Any] | None = None,
        *,
        max_tool_name: int = MAX_TOOL_NAME,
        client_info: types.Implementation | None = None,
        stop_timeout_s: float = 5.0,
    ) -> None:
        if registry is None:
            registry = ServerRegistry()
        elif not isinstance(registry, ServerRegistry):
            registry = ServerRegistry.from_mapping(registry)
        self._registry = registry
        self._max_tool_name = max_tool_name
        self._client_info = client_info or types.Implementation(name="autobrowser", version="0")
        self._stop_timeout_s = stop_timeout_s
        self._servers: dict[str, ManagedServer] = {
            name: ManagedServer(name=name, config=config) for name, config in registry.all().items()
        }
        self._tool_index: dict[str, tuple[str, str]] = {}
        self._prompt_index: dict[str, tuple[str, str]] = {}
        self._resource_index: dict[str, list[str]] = {}
        self._catalog_version = 0
        self._closed = False

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> MCPManager:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.shutdown()

    async def start(self) -> None:
        """Connect all registered servers in parallel.

        A failed server does not block the others: the manager comes up partially ready,
        failures are visible in :meth:`status` and are retried lazily on the next call.
        """

        self._closed = False
        servers = list(self._servers.values())
        results = await asyncio.gather(*(self._connect(s) for s in servers), return_exceptions=True)
        for managed, result in zip(servers, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("MCP server %s failed to start: %s", managed.name, _describe(result))

    async def add_server(self, name: str, config: Any, *, connect: bool = True) -> None:
        """Register a server at runtime (no harness restart, no new code)."""

        validate_server_name(name)
        if name in self._servers:
            raise ValueError(f"MCP server {name!r} is already registered")
        typed = parse_server_config(config)
        self._registry.add(name, typed)
        self._servers[name] = ManagedServer(name=name, config=typed)
        if connect and not self._closed:
            await self._connect(self._servers[name])  # on failure: FAILED + exception to caller

    async def remove_server(self, name: str, timeout: float | None = None) -> None:
        managed = self._get(name)
        await self._close(managed, self._timeout(timeout))
        self._servers.pop(name, None)
        self._registry.remove(name)
        self._rebuild_indexes()

    async def reconnect(self, name: str, timeout: float | None = None) -> None:
        """Manually restart one server (new generation). Raises if it cannot connect."""

        managed = self._get(name)
        await self._close(managed, self._timeout(timeout))
        if self._closed:
            raise ServerClosedError(name)
        await self._connect(managed)

    async def refresh(self, name: str | None = None) -> None:
        """Re-run discovery now (all servers or one). Needed for ephemeral servers,
        which cannot receive ``list_changed`` notifications."""

        targets = [self._get(name)] if name is not None else list(self._servers.values())
        for managed in targets:
            if managed.ephemeral:
                async with managed.lifecycle_lock:
                    if not managed.closing and not self._closed:
                        with contextlib.suppress(ServerUnavailableError, ServerClosedError):
                            await self._probe_ephemeral(managed)
                continue
            if managed.state is not ConnectionState.READY:
                continue
            self._schedule_rediscover(managed, *ALL_KINDS)
            task = managed.rediscover_task
            if task is not None:
                await asyncio.wait({task})

    async def shutdown(self, timeout: float | None = None) -> None:
        """Close every connection in parallel, each bounded by ``timeout``."""

        self._closed = True
        await asyncio.gather(
            *(self._close(s, self._timeout(timeout)) for s in list(self._servers.values())),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------ catalog (sync reads)

    def list_tools(self) -> list[ToolDescriptor]:
        """What may be offered to the LLM right now: tools of READY servers only."""

        return [t for s in self._ready_servers() for t in s.tools.values()]

    def list_resources(self) -> list[ResourceDescriptor]:
        return [r for s in self._ready_servers() for r in s.resources.values()]

    def list_resource_templates(self) -> list[ResourceTemplateDescriptor]:
        return [t for s in self._ready_servers() for t in s.resource_templates]

    def list_prompts(self) -> list[PromptDescriptor]:
        return [p for s in self._ready_servers() for p in s.prompts.values()]

    def get_tool(self, qualified_name: str) -> ToolDescriptor:
        route = self._tool_index.get(qualified_name)
        if route is None:
            raise UnknownToolError(qualified_name)
        server, local = route
        return self._servers[server].tools[local]

    def servers(self) -> list[str]:
        return list(self._servers)

    def server_config(self, name: str) -> Any:
        return self._get(name).config

    def generation(self, name: str) -> int:
        return self._get(name).generation

    @property
    def catalog_version(self) -> int:
        """Bumped whenever what ``list_*`` returns may have changed (rediscovery, a server
        becoming READY or dropping out). Lets the harness cheaply refresh its tool list."""

        return self._catalog_version

    def status(self) -> dict[str, ServerStatus]:
        return {name: self._status(s) for name, s in self._servers.items()}

    # ------------------------------------------------------------------ routing / calls

    async def call_tool(
        self,
        qualified_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
        expected_generation: int | None = None,
        progress_callback: Any = None,
    ) -> types.CallToolResult:
        """Call a tool by its qualified name. Never retried automatically.

        ``expected_generation``: pin the server incarnation the caller planned against
        (e.g. the browser session of the current task). If the server was restarted in the
        meantime, :class:`ServerStateLostError` is raised *before* anything is sent.
        """

        route = self._tool_index.get(qualified_name)
        if route is None:
            raise UnknownToolError(qualified_name)
        server_name, local_name = route
        managed = self._get(server_name)
        timeout = float(timeout_s if timeout_s is not None else managed.config.call_timeout_s)
        args = dict(arguments or {})

        def op(session: ClientSession) -> Coroutine[Any, Any, types.CallToolResult]:
            return session.call_tool(
                local_name,
                args,
                read_timeout_seconds=timedelta(seconds=timeout),
                progress_callback=progress_callback,
            )

        return await self._request(
            managed,
            op,
            idempotent=False,
            wait_s=timeout,
            expected_generation=expected_generation,
        )

    async def read_resource(self, uri: str, *, server: str | None = None) -> types.ReadResourceResult:
        if server is None:
            owners = self._resource_index.get(uri, [])
            if not owners:
                raise UnknownResourceError(f"{uri} (URIs from templates need an explicit server=)")
            if len(owners) > 1:
                raise AmbiguousResourceError(uri, owners)
            server = owners[0]
        managed = self._get(server)
        return await self._request(
            managed,
            lambda s: s.read_resource(AnyUrl(uri)),
            idempotent=True,
            wait_s=managed.config.request_timeout_s,
        )

    async def get_prompt(
        self, qualified_name: str, arguments: Mapping[str, str] | None = None
    ) -> types.GetPromptResult:
        route = self._prompt_index.get(qualified_name)
        if route is None:
            raise UnknownPromptError(qualified_name)
        server_name, local_name = route
        managed = self._get(server_name)
        args = {k: str(v) for k, v in (arguments or {}).items()}
        return await self._request(
            managed,
            lambda s: s.get_prompt(local_name, args),
            idempotent=True,
            wait_s=managed.config.request_timeout_s,
        )

    async def ping(self, name: str) -> None:
        managed = self._get(name)
        await self._request(
            managed, lambda s: s.send_ping(), idempotent=True, wait_s=managed.config.ping_timeout_s
        )

    # ------------------------------------------------------------------ internal: requests

    async def _request(
        self,
        managed: ManagedServer,
        op: SessionOp[T],
        *,
        idempotent: bool,
        wait_s: float,
        expected_generation: int | None = None,
    ) -> T:
        attempts = 2 if idempotent else 1
        for attempt in range(1, attempts + 1):
            if self._closed or (managed.closing and managed.state is ConnectionState.DISCONNECTED):
                raise ServerClosedError(managed.name)
            if managed.ephemeral:
                runner: Coroutine[Any, Any, T] = self._run_ephemeral(managed, op)
                generation = managed.generation
            else:
                await self._ensure_ready(managed, wait_s)
                generation = managed.generation
                if expected_generation is not None and generation != expected_generation:
                    raise ServerStateLostError(managed.name, expected_generation, generation)
                session = managed.session
                if session is None:  # lost between the READY check and here
                    raise ServerUnavailableError(managed.name, managed.last_error)
                runner = op(session)

            task: asyncio.Task[T] = asyncio.create_task(runner)
            managed.inflight.add(task)
            task.add_done_callback(managed.inflight.discard)
            try:
                return await task
            except asyncio.CancelledError:
                if _cancelling():  # the caller itself was cancelled
                    raise
                if not managed.closing and not self._closed and not managed.ephemeral:
                    # aborted because its connection incarnation died
                    raise ServerConnectionLostError(
                        managed.name, generation, bool(managed.config.stateful)
                    ) from None
                raise ServerClosedError(managed.name) from None  # aborted by close/shutdown
            except TRANSPORT_ERRORS as exc:
                lost: BaseException = exc
            except McpError as exc:
                if _is_timeout(exc):
                    self._schedule_liveness_check(managed)
                    raise RequestTimeoutError(managed.name, exc.error.message) from exc
                if not self._is_connection_closed(managed, exc):
                    raise  # protocol error: unchanged
                lost = exc
            # only a dropped transport gets here
            managed.last_error = _describe(lost)
            self._mark_transport_lost(managed)
            if attempt == attempts:
                raise ServerConnectionLostError(
                    managed.name, generation, bool(managed.config.stateful)
                ) from lost
        raise AssertionError("unreachable")  # pragma: no cover

    def _is_connection_closed(self, managed: ManagedServer, exc: McpError) -> bool:
        if exc.error.code != types.CONNECTION_CLOSED:
            return False
        # -32000 is also the generic "server error" code servers may use themselves;
        # only the SDK's own "Connection closed" (or an observed transport EOF) counts.
        message = (exc.error.message or "").lower()
        owner = managed.owner_task
        return "connection closed" in message or managed.wake.is_set() or (owner is not None and owner.done())

    async def _ensure_ready(self, managed: ManagedServer, wait_s: float) -> None:
        if managed.state is ConnectionState.READY:
            return
        if managed.state is ConnectionState.RECONNECTING:
            task = managed.reconnect_task
            if task is not None and not task.done():
                # wait for the supervisor's attempts instead of starting a parallel connect
                await asyncio.wait({task}, timeout=max(wait_s, managed.config.init_timeout_s))
        elif managed.state in (
            ConnectionState.DISCONNECTED,
            ConnectionState.FAILED,
            ConnectionState.CONNECTING,
        ):
            with contextlib.suppress(ServerUnavailableError, ServerClosedError):
                await self._connect(managed)  # single-flight under lifecycle_lock
        if managed.state is not ConnectionState.READY:
            raise ServerUnavailableError(managed.name, managed.last_error)

    # ------------------------------------------------------------------ internal: lifecycle

    async def _connect(self, managed: ManagedServer) -> None:
        async with managed.lifecycle_lock:
            if managed.state in (ConnectionState.READY, ConnectionState.RECONNECTING):
                return
            if self._closed:
                raise ServerClosedError(managed.name)
            managed.closing = False
            self._set_state(managed, ConnectionState.CONNECTING)
            try:
                if managed.ephemeral:
                    await self._probe_ephemeral(managed)
                else:
                    await self._start_owner(managed)
            except BaseException:
                if managed.state is ConnectionState.CONNECTING:
                    self._set_state(
                        managed,
                        ConnectionState.DISCONNECTED if managed.closing else ConnectionState.FAILED,
                    )
                raise

    async def _start_owner(self, managed: ManagedServer) -> None:
        """Spawn a new owner task (one connection incarnation) and wait until it is ready."""

        ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        wake = asyncio.Event()
        managed.stop_requested = False
        managed.wake = wake
        task = asyncio.create_task(self._owner(managed, ready, wake), name=f"mcp-owner:{managed.name}")
        managed.owner_task = task
        task.add_done_callback(functools.partial(self._on_owner_exit, managed))
        try:
            session, init, catalog = await ready
        except asyncio.CancelledError:
            if _cancelling():
                # our caller was cancelled: do not leave a half-initialized owner behind
                managed.stop_requested = True
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                raise
            raise ServerClosedError(managed.name) from None
        except Exception as exc:
            raise ServerUnavailableError(managed.name, managed.last_error or _describe(exc)) from exc

        managed.session = session
        managed.server_info = init.serverInfo
        managed.capabilities = init.capabilities
        self._apply_catalog(managed, catalog)
        managed.catalog_stale = False
        managed.generation += 1
        managed.reconnect_attempts = 0
        managed.last_error = None
        self._set_state(managed, ConnectionState.READY)
        self._rebuild_indexes()
        logger.info("MCP server %s ready (generation %d)", managed.name, managed.generation)
        if managed.dirty:  # list_changed arrived during the initial discovery
            self._schedule_rediscover(managed)

    async def _owner(self, managed: ManagedServer, ready: asyncio.Future[Any], wake: asyncio.Event) -> None:
        """The only task that enters and exits the transport/session contexts."""

        cfg = managed.config
        session_ref: ClientSession | None = None
        relay_send, relay_recv = anyio.create_memory_object_stream[Any](0)
        try:
            async with open_transport(cfg) as streams:
                read_stream, write_stream = streams[0], streams[1]
                async with anyio.create_task_group() as tg:
                    tg.start_soon(self._pump, managed, read_stream, relay_send, wake)
                    try:
                        async with ClientSession(
                            relay_recv,
                            write_stream,
                            read_timeout_seconds=timedelta(seconds=cfg.request_timeout_s),
                            message_handler=self._make_message_handler(managed, wake),
                            client_info=self._client_info,
                        ) as session:
                            session_ref = session
                            with anyio.fail_after(cfg.init_timeout_s):
                                init = await session.initialize()
                                catalog = await discover(managed.name, session, init.capabilities, ALL_KINDS)
                            if not ready.done():
                                ready.set_result((session, init, catalog))
                            await wake.wait()  # hold the connection until stop / transport loss
                            if not managed.stop_requested and managed.inflight:
                                # transport lost: give the receive loop a moment to answer
                                # pending requests with CONNECTION_CLOSED before we tear down
                                await asyncio.wait(set(managed.inflight), timeout=_LOST_GRACE_S)
                    finally:
                        tg.cancel_scope.cancel()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as exc:
            if not managed.stop_requested:
                managed.last_error = _describe(exc)
            if not ready.done():
                leaf = _leaf(exc)
                ready.set_exception(leaf if isinstance(leaf, Exception) else RuntimeError(_describe(exc)))
            if not isinstance(exc, Exception):
                raise
        finally:
            relay_send.close()
            relay_recv.close()
            if session_ref is not None and managed.session is session_ref:
                managed.session = None

    async def _pump(
        self,
        managed: ManagedServer,
        source: Any,
        sink: Any,
        wake: asyncio.Event,
    ) -> None:
        """Relay transport -> session and notice when the transport ends (server gone)."""

        try:
            async with source, sink:  # we own both ends like ClientSession would
                async for item in source:
                    await sink.send(item)
        except TRANSPORT_ERRORS:
            pass
        if not wake.is_set():  # EOF without a requested stop: the server went away
            managed.last_error = "transport closed by the server"
            wake.set()

    def _on_owner_exit(self, managed: ManagedServer, task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()  # retrieve, to avoid "exception was never retrieved"
        if managed.owner_task is not task:
            return
        if managed.stop_requested or managed.closing:
            return
        if managed.state is ConnectionState.READY:  # the owner ended on its own
            self._mark_transport_lost(managed)
        # requests of the dead incarnation must not wait for their full timeout
        for request_task in list(managed.inflight):
            request_task.cancel()

    def _mark_transport_lost(self, managed: ManagedServer) -> None:
        if managed.ephemeral or managed.state is not ConnectionState.READY or managed.closing:
            return
        managed.wake.set()  # the owner closes its contexts in its own task
        if managed.config.reconnect.enabled and not self._closed:
            self._set_state(managed, ConnectionState.RECONNECTING)
            managed.reconnect_attempts = 0
            managed.reconnect_task = asyncio.create_task(
                self._supervise_reconnect(managed), name=f"mcp-reconnect:{managed.name}"
            )
        else:
            self._set_state(managed, ConnectionState.FAILED)
        logger.warning(
            "MCP server %s lost its transport (generation %d): %s",
            managed.name,
            managed.generation,
            managed.last_error,
        )

    async def _supervise_reconnect(self, managed: ManagedServer) -> None:
        """Background reconnect with backoff. The lock is held per attempt, never during
        the sleep, so shutdown()/remove_server() can always interrupt the series."""

        policy = managed.config.reconnect
        old = managed.owner_task
        if old is not None and not old.done():
            _, pending = await asyncio.wait({old}, timeout=self._stop_timeout_s)
            if pending:
                old.cancel()
                await asyncio.wait({old})
        for attempt in range(1, policy.max_attempts + 1):
            managed.reconnect_attempts = attempt
            await asyncio.sleep(policy.delay_for(attempt))
            async with managed.lifecycle_lock:
                if managed.closing or self._closed or managed.state is not ConnectionState.RECONNECTING:
                    return
                try:
                    await self._start_owner(managed)
                    return
                except (ServerUnavailableError, ServerClosedError) as exc:
                    logger.info(
                        "MCP server %s reconnect attempt %d/%d failed: %s",
                        managed.name,
                        attempt,
                        policy.max_attempts,
                        exc,
                    )
        if managed.state is ConnectionState.RECONNECTING:
            self._set_state(managed, ConnectionState.FAILED)

    async def _close(self, managed: ManagedServer, timeout: float) -> None:
        managed.closing = True
        aux = [t for t in (managed.reconnect_task, managed.rediscover_task, managed.liveness_task) if t]
        for task in aux:
            if not task.done():
                task.cancel()
        owner = managed.owner_task
        if (
            managed.state in (ConnectionState.CONNECTING, ConnectionState.RECONNECTING)
            and owner is not None
            and not owner.done()
        ):
            owner.cancel()  # abort a connect in progress; contexts still exit in the owner
        pending_aux = {t for t in aux if not t.done()}
        if pending_aux:
            await asyncio.wait(pending_aux, timeout=timeout)
        async with managed.lifecycle_lock:
            await self._stop_owner(managed, timeout)
            managed.session = None
            self._set_state(managed, ConnectionState.DISCONNECTED)

    async def _stop_owner(self, managed: ManagedServer, timeout: float) -> None:
        task = managed.owner_task
        if task is None or task.done():
            return
        if managed.inflight:  # let requests finish, then cancel the rest
            _, pending = await asyncio.wait(set(managed.inflight), timeout=timeout / 2)
            for request_task in pending:
                request_task.cancel()
        managed.stop_requested = True
        managed.wake.set()
        # asyncio.wait never raises the owner's outcome (errors are in last_error)
        _, pending = await asyncio.wait({task}, timeout=timeout)
        if pending:
            task.cancel()  # exiting the contexts still happens inside the owner task
            await asyncio.wait({task})

    # ------------------------------------------------------------------ internal: ephemeral

    @contextlib.asynccontextmanager
    async def _open_session(self, managed: ManagedServer) -> AsyncIterator[tuple[ClientSession, Any]]:
        """Open transport + session + initialize in the *current* task (ephemeral mode)."""

        cfg = managed.config
        async with open_transport(cfg) as streams:
            async with ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=timedelta(seconds=cfg.request_timeout_s),
                client_info=self._client_info,
            ) as session:
                with anyio.fail_after(cfg.init_timeout_s):
                    init = await session.initialize()
                yield session, init

    async def _probe_ephemeral(self, managed: ManagedServer) -> None:
        """Discovery for an ephemeral server: connect, list, disconnect."""

        async def probe() -> tuple[Any, Catalog]:
            async with self._open_session(managed) as (session, init):
                with anyio.fail_after(managed.config.init_timeout_s):
                    catalog = await discover(managed.name, session, init.capabilities, ALL_KINDS)
                return init, catalog

        task = asyncio.create_task(probe(), name=f"mcp-probe:{managed.name}")
        managed.owner_task = task
        try:
            init, catalog = await task
        except asyncio.CancelledError:
            if _cancelling():
                raise
            raise ServerClosedError(managed.name) from None
        except Exception as exc:
            managed.last_error = _describe(exc)
            raise ServerUnavailableError(managed.name, managed.last_error) from exc
        managed.server_info = init.serverInfo
        managed.capabilities = init.capabilities
        self._apply_catalog(managed, catalog)
        managed.catalog_stale = False
        managed.generation += 1
        managed.last_error = None
        self._set_state(managed, ConnectionState.READY)
        self._rebuild_indexes()

    async def _run_ephemeral(self, managed: ManagedServer, op: SessionOp[T]) -> T:
        opened = False
        try:
            async with self._open_session(managed) as (session, _init):
                opened = True
                return await op(session)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            leaf = _leaf(exc)
            if not opened:
                managed.last_error = _describe(exc)
                raise ServerUnavailableError(managed.name, managed.last_error) from leaf
            if leaf is exc:
                raise
            raise leaf from exc

    # ------------------------------------------------------------------ internal: notifications

    def _make_message_handler(
        self, managed: ManagedServer, wake: asyncio.Event
    ) -> Callable[[Any], Awaitable[None]]:
        async def on_message(message: Any) -> None:
            # Runs inside the session's receive loop: never await responses from this same
            # server here (e.g. list_tools) — that would deadlock. Only schedule work.
            if wake.is_set():
                return  # this incarnation is stopping
            if isinstance(message, Exception):
                managed.last_error = _describe(message)
                self._schedule_liveness_check(managed)
                return
            if isinstance(message, types.ServerNotification):
                kind = _LIST_CHANGED.get(type(message.root))
                if kind is not None:
                    self._schedule_rediscover(managed, kind)
            # server->client requests (sampling/roots/elicitation) are out of scope

        return on_message

    def _schedule_rediscover(self, managed: ManagedServer, *kinds: str) -> None:
        managed.dirty.update(kinds)
        if managed.state is not ConnectionState.READY:
            return  # _start_owner picks up ``dirty`` once READY
        if managed.rediscover_task is None or managed.rediscover_task.done():
            managed.rediscover_task = asyncio.create_task(
                self._rediscover_loop(managed), name=f"mcp-rediscover:{managed.name}"
            )

    async def _rediscover_loop(self, managed: ManagedServer) -> None:
        while managed.dirty and managed.state is ConnectionState.READY:
            kinds, managed.dirty = frozenset(managed.dirty), set()  # coalesce a burst
            session = managed.session
            if session is None:
                return
            try:
                catalog = await discover(managed.name, session, managed.capabilities, kinds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                managed.last_error = _describe(exc)
                managed.catalog_stale = True  # keep the previous catalog
                self._schedule_liveness_check(managed)
            else:
                if managed.session is session:  # not a catalog of a replaced incarnation
                    self._apply_catalog(managed, catalog)
                    managed.catalog_stale = False
            self._rebuild_indexes()

    def _schedule_liveness_check(self, managed: ManagedServer) -> None:
        if managed.ephemeral:
            return
        if managed.liveness_task is None or managed.liveness_task.done():
            managed.liveness_task = asyncio.create_task(
                self._check_liveness(managed), name=f"mcp-liveness:{managed.name}"
            )

    async def _check_liveness(self, managed: ManagedServer) -> None:
        session, generation = managed.session, managed.generation
        if session is None or managed.state is not ConnectionState.READY:
            return
        try:
            with anyio.fail_after(managed.config.ping_timeout_s):
                await session.send_ping()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if managed.generation == generation and managed.session is session:
                managed.last_error = f"liveness check failed: {_describe(exc)}"
                self._mark_transport_lost(managed)

    # ------------------------------------------------------------------ internal: catalog/indexes

    @staticmethod
    def _apply_catalog(managed: ManagedServer, catalog: Catalog) -> None:
        if catalog.tools is not None:
            managed.tools = catalog.tools
        if catalog.resources is not None:
            managed.resources = catalog.resources
        if catalog.resource_templates is not None:
            managed.resource_templates = catalog.resource_templates
        if catalog.prompts is not None:
            managed.prompts = catalog.prompts

    def _rebuild_indexes(self) -> None:
        """Index the last known catalog of ALL servers (incl. failed ones): a call to a
        tool of a failed server triggers recovery in ``_ensure_ready``. ``list_tools()``
        still shows the LLM only READY servers — intentionally. Sorted iteration keeps
        qualified names deterministic across rebuilds."""

        tools: dict[str, tuple[str, str]] = {}
        prompts: dict[str, tuple[str, str]] = {}
        resources: dict[str, list[str]] = {}
        for server_name in sorted(self._servers):
            managed = self._servers[server_name]
            for local in sorted(managed.tools):
                descriptor = managed.tools[local]
                descriptor.qualified_name = qualify(server_name, local, tools, max_len=self._max_tool_name)
                tools[descriptor.qualified_name] = (server_name, local)
            for local in sorted(managed.prompts):
                descriptor_p = managed.prompts[local]
                descriptor_p.qualified_name = qualify(
                    server_name, local, prompts, max_len=self._max_tool_name
                )
                prompts[descriptor_p.qualified_name] = (server_name, local)
            for uri in managed.resources:
                resources.setdefault(uri, []).append(server_name)
        self._tool_index, self._prompt_index, self._resource_index = tools, prompts, resources
        self._catalog_version += 1

    def _set_state(self, managed: ManagedServer, state: ConnectionState) -> None:
        was_ready = managed.state is ConnectionState.READY
        managed.state = state
        if was_ready != (state is ConnectionState.READY):
            self._catalog_version += 1

    def _ready_servers(self) -> Iterable[ManagedServer]:
        return (s for s in self._servers.values() if s.state is ConnectionState.READY)

    def _status(self, managed: ManagedServer) -> ServerStatus:
        info = managed.server_info
        return ServerStatus(
            state=managed.state,
            generation=managed.generation,
            last_error=managed.last_error,
            catalog_stale=managed.catalog_stale,
            reconnect_attempts=managed.reconnect_attempts,
            stateful=bool(managed.config.stateful),
            connection_mode=managed.config.connection_mode,
            server_name=getattr(info, "name", None),
            server_version=getattr(info, "version", None),
        )

    def _timeout(self, timeout: float | None) -> float:
        return self._stop_timeout_s if timeout is None else float(timeout)

    def _get(self, name: str) -> ManagedServer:
        try:
            return self._servers[name]
        except KeyError:
            raise UnknownServerError(f"unknown MCP server {name!r}") from None


__all__ = ["MCPManager", "ManagedServer"]
