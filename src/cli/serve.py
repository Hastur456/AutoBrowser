"""``autobrowser serve``: compose the stand (``docs/development/serve-mode.md``).

The only place that wires the stand: settings and start checks, the worker session (its own
browser profile and CDP port, approvals only with ``serve.api_approvals``), the stand's event
stream, ledger, job state, channels, dispatcher and runtime. ``src/serve`` itself never
imports the CLI, the harness or the providers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import uvicorn

from src.agent_loop.events import EventEmitter, JsonlEventSink
from src.agent_loop.goals import GoalLimits
from src.cli.bootstrap import USAGE_EXIT_CODE, build_session
from src.cli.parser import build_parser
from src.config import ServeSettings, Settings, get_settings
from src.harness.approvals import ApprovalBroker
from src.serve.app import build_app
from src.serve.approvals import ApprovalBridge
from src.serve.checks import startup_problems
from src.serve.clock import SystemClock, resolve_timezone
from src.serve.dispatcher import Dispatcher
from src.serve.ledger import Ledger
from src.serve.notify import LogChannel, NotificationChannel, Notifier, WebhookOutChannel
from src.serve.paths import ServePaths, serve_paths
from src.serve.runtime import ServeRuntime, ServeStartError
from src.serve.scheduler import Scheduler
from src.serve.state import JobStateStore
from src.serve.tally import RunTally
from src.ui.controller import SessionController

#: Session id prefix of the stand's own event stream.
SERVE_SESSION_PREFIX = "serve-"
#: Labels of the worker session (``session.started``), so its traces are found by them.
WORKER_LABELS = {"mode": "serve", "worker": "w0"}


def _err(line: str) -> None:
    print(f"autobrowser serve: {line}", file=sys.stderr, flush=True)


def run_serve_cli(args: argparse.Namespace, write: Callable[[str], None] = print) -> int:
    """Check the configuration and, unless ``--check``, run the stand until Ctrl+C/SIGTERM."""

    try:
        settings = get_settings()
    except ValueError as exc:
        _err(f"invalid configuration: {exc}")
        return USAGE_EXIT_CODE
    problems = startup_problems(settings)
    for problem in problems:
        _err(problem)
    if problems:
        return USAGE_EXIT_CODE
    write(summary(settings))
    if getattr(args, "check", False):
        return 0
    try:
        return asyncio.run(_serve(settings, write))
    except KeyboardInterrupt:
        return 0


def summary(settings: Settings) -> str:
    """One line about the stand, never a secret."""

    serve = settings.serve
    token = "token set" if serve.token is not None and serve.token.get_secret_value().strip() else "no token"
    approvals = "approvals via the API" if serve.api_approvals else "approvals denied (dont_ask)"
    return (
        f"serve: {serve.host}:{serve.port}, {len(serve.jobs)} jobs, {token}, "
        f"channels {', '.join(serve.channels)}, {approvals}, browser launch {settings.browser.launch}"
    )


def worker_args(serve: ServeSettings, paths: ServePaths) -> argparse.Namespace:
    """Session arguments of worker ``w0``: the CLI defaults plus its own profile and port."""

    args = build_parser().parse_args([])
    args.user_data_dir = str(paths.worker_profile(0))
    args.cdp_port = serve.cdp_port_base
    args.labels = dict(WORKER_LABELS)
    # Without an approval channel nobody answers: a configured default becomes dont_ask.
    args.interactive = serve.api_approvals
    return args


def build_channels(
    serve: ServeSettings, events: EventEmitter, write: Callable[[str], None]
) -> list[NotificationChannel]:
    channels: list[NotificationChannel] = []
    if "log" in serve.channels:
        channels.append(LogChannel(events, write))
    if "webhook" in serve.channels and serve.webhook_out is not None:
        channels.append(WebhookOutChannel(serve.webhook_out))
    return channels


async def close_channels(channels: list[NotificationChannel]) -> None:
    for channel in channels:
        close = getattr(channel, "aclose", None)
        if close is not None:
            await close()


async def _serve(
    settings: Settings, write: Callable[[str], None], *, until: Awaitable[Any] | None = None
) -> int:
    """Run the stand until ``until`` finishes (default: never, so Ctrl+C or SIGTERM)."""

    serve = settings.serve
    paths = serve_paths(serve, settings.storage)
    tz = resolve_timezone(serve.timezone)
    events = EventEmitter(
        JsonlEventSink(paths.events), session_id=f"{SERVE_SESSION_PREFIX}{uuid4().hex[:8]}"
    )
    tally = RunTally()
    clock = SystemClock(tz)
    states = JobStateStore(paths.root)
    broker = (
        ApprovalBroker(
            settings.permissions,
            progress_timeout_seconds=GoalLimits.from_settings(settings.loop).progress_timeout_seconds,
        )
        if serve.api_approvals
        else None
    )
    session = build_session(
        worker_args(serve, paths), human_input=broker, event_sinks=(tally,), output_fn=write
    )
    controller = SessionController(session, broker=broker)
    channels = build_channels(serve, events, write)
    dispatcher = Dispatcher(
        serve,
        controller,
        ledger=Ledger(paths.ledger, tz),
        states=states,
        notifier=Notifier(channels, events, max_chars=serve.notify_max_chars),
        events=events,
        tally=tally,
        clock=clock,
    )
    scheduler = Scheduler(
        serve, dispatcher.fire, skip=dispatcher.skip, clock=clock, states=states, tz=tz
    )
    bridge = ApprovalBridge(controller, dispatcher.notifier, dispatcher) if broker is not None else None
    app = build_app(serve, dispatcher, events=events, clock=clock, approvals=bridge)
    runtime = ServeRuntime(
        serve, controller, dispatcher=dispatcher, events=events, services=[scheduler.run]
    )
    if bridge is not None:
        bridge.start()
    try:
        # The HTTP server ends the stand: uvicorn returns on SIGINT/SIGTERM (Ctrl+C on Windows).
        await runtime.serve(until=until if until is not None else http_server(app, serve).serve())
    except ServeStartError as exc:
        _err(str(exc))
        return USAGE_EXIT_CODE
    finally:
        if bridge is not None:
            bridge.stop()
        await close_channels(channels)
        await session.close()
    return 0


def http_server(app: Any, serve: ServeSettings) -> uvicorn.Server:
    """The API server: no access log (it would write client addresses), warnings only."""

    return uvicorn.Server(
        uvicorn.Config(
            app,
            host=serve.host,
            port=serve.port,
            access_log=False,
            log_level="warning",
            lifespan="off",
        )
    )


__all__ = ["build_channels", "http_server", "run_serve_cli", "summary", "worker_args"]
