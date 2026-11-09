"""``autobrowser serve``: compose the stand (``docs/development/serve-mode.md``).

The only place that wires the stand: settings and start checks, one session per worker (its
own browser profile and CDP port, approvals only with an approval channel), the stand's event
stream, ledger, job state, channels (the Telegram adapter of ``src/channels`` included),
dispatcher, command port and runtime. ``src/serve`` itself never imports the CLI, the
harness, the providers or the channels.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import uvicorn

from src.agent_loop.events import EventEmitter, JsonlEventSink
from src.agent_loop.goals import GoalLimits
from src.channels.telegram import BotApi, TelegramChannel, TelegramError
from src.cli import models
from src.cli.bootstrap import USAGE_EXIT_CODE, build_session
from src.cli.output import config_error_lines
from src.cli.parser import build_parser
from src.config import ServeSettings, Settings, get_settings
from src.harness.approvals import ApprovalBroker
from src.serve.app import build_app
from src.serve.approvals import ApprovalBridge, ApprovalDesk
from src.serve.checks import startup_problems, startup_warnings
from src.serve.clock import Clock, SystemClock, resolve_timezone
from src.serve.commands import OffsetStore, ServeCommands
from src.serve.dispatcher import Dispatcher
from src.serve.ledger import Ledger
from src.serve.notify import LogChannel, NotificationChannel, Notifier, WebhookOutChannel
from src.serve.paths import WORKER_PROFILE, ServePaths, serve_paths
from src.serve.pool import WorkerPool
from src.serve.runtime import ServeRuntime, ServeServiceError, ServeStartError
from src.serve.scheduler import Scheduler
from src.serve.state import ChannelStateStore, JobStateStore
from src.serve.tally import RunTally
from src.ui.controller import SessionController

#: Exit code when a part of the running stand died (the dispatcher, the scheduler).
SERVICE_FAILED_EXIT_CODE = 1
#: Session id prefix of the stand's own event stream.
SERVE_SESSION_PREFIX = "serve-"
#: Labels of a worker session (``session.started``), so its traces are found by them.
SERVE_MODE_LABEL = {"mode": "serve"}


def _err(line: str) -> None:
    print(f"autobrowser serve: {line}", file=sys.stderr, flush=True)


def run_serve_cli(args: argparse.Namespace, write: Callable[[str], None] = print) -> int:
    """Check the configuration and, unless ``--check``, run the stand until Ctrl+C/SIGTERM."""

    try:
        settings = get_settings()
    except ValueError as exc:
        for line in config_error_lines(exc):
            _err(f"invalid configuration: {line}")
        return USAGE_EXIT_CODE
    problems = [*startup_problems(settings), *models.tier_problems(settings)]
    for problem in problems:
        _err(problem)
    if problems:
        return USAGE_EXIT_CODE
    for warning in startup_warnings(settings):
        _err(f"warning: {warning}")
    write(summary(settings))
    if getattr(args, "check", False):
        return 0
    try:
        return asyncio.run(_serve(settings, write))
    except KeyboardInterrupt:
        return 0


def approval_channel(serve: ServeSettings) -> bool:
    """Someone can answer an approval: the API (``api_approvals``) or the chat buttons."""

    telegram = serve.telegram
    return serve.api_approvals or (
        "telegram" in serve.channels and telegram is not None and telegram.approvals
    )


def summary(settings: Settings) -> str:
    """One line about the stand, never a secret."""

    serve = settings.serve
    token = "token set" if serve.token is not None and serve.token.get_secret_value().strip() else "no token"
    via = [
        name
        for name, on in (
            ("the API", serve.api_approvals),
            ("telegram", approval_channel(serve) and "telegram" in serve.channels
             and serve.telegram is not None and serve.telegram.approvals),
        )
        if on
    ]
    approvals = f"approvals via {' and '.join(via)}" if via else "approvals denied (dont_ask)"
    chats = (
        f", telegram ({len(serve.telegram.allowed_chat_ids)} chats)"
        if "telegram" in serve.channels and serve.telegram is not None
        else ""
    )
    cascade = settings.cascade
    tiers = f", cascade {len(cascade.tiers)} tiers" if cascade.enabled and cascade.tiers else ""
    workers = f", {serve.workers} workers" if serve.workers > 1 else ""
    return (
        f"serve: {serve.host}:{serve.port}, {len(serve.jobs)} jobs, {token}, "
        f"channels {', '.join(serve.channels)}, {approvals}, browser launch {settings.browser.launch}"
        f"{tiers}{chats}{workers}"
    )


def worker_args(serve: ServeSettings, paths: ServePaths, index: int = 0) -> argparse.Namespace:
    """Session arguments of worker ``w<index>``: the CLI defaults plus its own profile
    (``<profiles_dir>/w<index>``, also ``{profile_dir}``) and CDP port (``cdp_port_base +
    index``)."""

    args = build_parser().parse_args([])
    args.user_data_dir = str(paths.worker_profile(index))
    args.cdp_port = serve.cdp_port_base + index
    args.labels = {**SERVE_MODE_LABEL, "worker": WORKER_PROFILE.format(index=index)}
    # Without an approval channel nobody answers: a configured default becomes dont_ask.
    args.interactive = approval_channel(serve)
    return args


def build_channels(
    serve: ServeSettings,
    events: EventEmitter,
    write: Callable[[str], None],
    *,
    clock: Clock | None = None,
    offsets: OffsetStore | None = None,
) -> list[NotificationChannel]:
    channels: list[NotificationChannel] = []
    if "log" in serve.channels:
        channels.append(LogChannel(events, write))
    if "webhook" in serve.channels and serve.webhook_out is not None:
        channels.append(WebhookOutChannel(serve.webhook_out))
    if "telegram" in serve.channels and serve.telegram is not None:
        if clock is None or offsets is None:
            raise ValueError("the telegram channel needs the stand's clock and offset store")
        channels.append(
            TelegramChannel(serve.telegram, api=BotApi(serve.telegram), offsets=offsets, clock=clock)
        )
    return channels


def telegram_channel(channels: list[NotificationChannel]) -> TelegramChannel | None:
    return next((channel for channel in channels if isinstance(channel, TelegramChannel)), None)


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
    channels = build_channels(serve, events, write, clock=clock, offsets=ChannelStateStore(paths.root))
    telegram = telegram_channel(channels)
    if telegram is not None:
        # Before any browser starts: a bad token or a webhook on the bot refuses the stand.
        try:
            await telegram.check()
        except TelegramError as exc:
            _err(f"the telegram channel cannot start: {exc}")
            await close_channels(channels)
            if inspect.iscoroutine(until):
                until.close()  # never started: nothing waits for it
            return USAGE_EXIT_CODE
    # One session per worker, each with its own approval broker (a broker binds one task).
    brokers: list[ApprovalBroker | None] = []
    sessions = []
    controllers: list[SessionController] = []
    for index in range(serve.workers):
        broker = (
            ApprovalBroker(
                settings.permissions,
                progress_timeout_seconds=GoalLimits.from_settings(settings.loop).progress_timeout_seconds,
            )
            if approval_channel(serve)
            else None
        )
        session = build_session(
            worker_args(serve, paths, index), human_input=broker, event_sinks=(tally,), output_fn=write
        )
        brokers.append(broker)
        sessions.append(session)
        controllers.append(SessionController(session, broker=broker))
    pool = WorkerPool.from_settings(controllers, serve)
    dispatcher = Dispatcher(
        serve,
        pool,
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
    bridges = [
        ApprovalBridge(controller, dispatcher.notifier, dispatcher, index=index)
        for index, (controller, broker) in enumerate(zip(controllers, brokers, strict=True))
        if broker is not None
    ]
    desk = ApprovalDesk(bridges) if bridges else None
    app = build_app(serve, dispatcher, events=events, clock=clock, approvals=desk)
    services = [scheduler.run]
    if telegram is not None:
        chat_approvals = desk if serve.telegram is not None and serve.telegram.approvals else None
        commands = ServeCommands(serve, dispatcher, approvals=chat_approvals, events=events, clock=clock)

        async def telegram_polling() -> None:
            await telegram.run(commands)

        services.append(telegram_polling)
    runtime = ServeRuntime(serve, pool, dispatcher=dispatcher, events=events, services=services)
    for bridge in bridges:
        bridge.start()
    try:
        # The HTTP server ends the stand: uvicorn returns on SIGINT/SIGTERM (Ctrl+C on Windows).
        await runtime.serve(until=until if until is not None else http_server(app, serve).serve())
    except ServeStartError as exc:
        _err(str(exc))
        return USAGE_EXIT_CODE
    except ServeServiceError as exc:
        _err(str(exc))
        return SERVICE_FAILED_EXIT_CODE
    finally:
        for bridge in bridges:
            bridge.stop()
        await close_channels(channels)
        for session in sessions:
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


__all__ = [
    "approval_channel",
    "build_channels",
    "config_error_lines",
    "http_server",
    "run_serve_cli",
    "summary",
    "telegram_channel",
    "worker_args",
]
