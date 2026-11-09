"""``autobrowser serve`` and ``doctor`` with the Telegram channel and the worker pool: the
channel composed by the CLI, checked before any browser starts, its token printed nowhere; one
session per worker, each with its own profile, port, label and approval broker."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.channels import telegram as telegram_module
from src.channels.telegram import TelegramUnauthorized, WebhookInfo
from src.cli import entry
from src.config import get_settings

TOKEN = "123456:SECRET-bot-token"


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


def with_telegram(monkeypatch: pytest.MonkeyPatch, *, chats: str = "[42]", approvals: bool = True,
                  jobs: str | None = None) -> None:
    monkeypatch.setenv("AUTOBROWSER_SERVE__CHANNELS", '["log", "telegram"]')
    monkeypatch.setenv("AUTOBROWSER_SERVE__TELEGRAM__BOT_TOKEN", TOKEN)
    monkeypatch.setenv("AUTOBROWSER_SERVE__TELEGRAM__ALLOWED_CHAT_IDS", chats)
    monkeypatch.setenv("AUTOBROWSER_SERVE__TELEGRAM__APPROVALS", str(approvals).lower())
    if jobs is not None:
        monkeypatch.setenv("AUTOBROWSER_SERVE__JOBS", jobs)
    get_settings.cache_clear()


def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: Any, method: str, payload: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"no Bot API call expected ({method})")

    monkeypatch.setattr(telegram_module.BotApi, "_call", refuse)


def webhook(monkeypatch: pytest.MonkeyPatch, *, is_set: bool = False, error: Exception | None = None) -> list[int]:
    calls: list[int] = []

    async def info(self: Any) -> WebhookInfo:
        calls.append(1)
        if error is not None:
            raise error
        return WebhookInfo(set=is_set, pending_updates=2)

    async def me(self: Any) -> str:
        return "PriceBot"

    monkeypatch.setattr(telegram_module.BotApi, "get_webhook_info", info)
    monkeypatch.setattr(telegram_module.BotApi, "get_me", me)
    return calls


# -- serve --check --------------------------------------------------------------------------


def test_serve_check_with_telegram_stays_offline_and_hides_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with_telegram(monkeypatch)
    no_network(monkeypatch)

    assert entry.main(["serve", "--check"]) == 0

    captured = capsys.readouterr()
    assert "telegram (1 chats)" in captured.out
    assert "approvals via telegram" in captured.out
    assert TOKEN not in captured.out + captured.err


def test_serve_check_refuses_telegram_without_chats_and_never_prints_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with_telegram(monkeypatch, chats="[]")

    assert entry.main(["serve", "--check"]) == 2

    captured = capsys.readouterr()
    assert "allowed_chat_ids" in captured.err
    assert TOKEN not in captured.out + captured.err


def test_the_worker_answers_approvals_with_the_chat_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.cli.serve import approval_channel, worker_args
    from src.serve.paths import serve_paths

    with_telegram(monkeypatch)
    settings = get_settings()

    assert approval_channel(settings.serve) is True
    assert worker_args(settings.serve, serve_paths(settings.serve, settings.storage)).interactive is True

    with_telegram(monkeypatch, approvals=False)
    assert approval_channel(get_settings().serve) is False


def test_serve_builds_the_telegram_channel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.agent_loop.events import EventEmitter
    from src.cli.serve import build_channels, telegram_channel
    from src.serve.state import ChannelStateStore
    from tests.serve.serve_fakes import FakeClock

    with_telegram(monkeypatch)
    serve = get_settings().serve

    channels = build_channels(serve, EventEmitter(), print, clock=FakeClock(), offsets=ChannelStateStore(tmp_path))

    assert [channel.name for channel in channels] == ["log", "telegram"]
    assert telegram_channel(channels) is channels[1]
    with pytest.raises(ValueError):
        build_channels(serve, EventEmitter(), print)


# -- serve start ----------------------------------------------------------------------------


def _fake_session(built: list[Any]) -> Any:
    from src.harness.session import SessionRuntime
    from tests.test_harness_session import llm_factory, make_config, no_mcp_runtime, no_start, noop_wait

    def build_session(args: Any, *, human_input: Any, event_sinks: Any, output_fn: Any) -> SessionRuntime:
        built.append((args, human_input))
        return SessionRuntime(
            make_config(), llm_factory=llm_factory, start_chrome_cdp=no_start, wait_for_port=noop_wait,
            mcp_runtime_factory=no_mcp_runtime, human_input=human_input, event_sinks=list(event_sinks),
        )

    return build_session


@pytest.mark.asyncio
async def test_a_bot_with_a_webhook_refuses_the_stand_before_any_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.cli import serve as serve_cli

    with_telegram(monkeypatch)
    webhook(monkeypatch, is_set=True)
    built: list[Any] = []
    monkeypatch.setattr(serve_cli, "build_session", _fake_session(built))

    code = await serve_cli._serve(get_settings(), lambda _line: None, until=asyncio.sleep(0))

    assert code == 2
    assert built == []
    err = capsys.readouterr().err
    assert "webhook" in err and TOKEN not in err


@pytest.mark.asyncio
async def test_a_bad_token_refuses_the_stand(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from src.cli import serve as serve_cli

    with_telegram(monkeypatch)
    webhook(monkeypatch, error=TelegramUnauthorized("getWebhookInfo", 401))
    built: list[Any] = []
    monkeypatch.setattr(serve_cli, "build_session", _fake_session(built))

    assert await serve_cli._serve(get_settings(), lambda _line: None, until=asyncio.sleep(0)) == 2
    assert built == []


@pytest.mark.asyncio
async def test_the_stand_polls_telegram_and_hands_the_worker_a_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.cli import serve as serve_cli

    with_telegram(monkeypatch)
    webhook(monkeypatch)
    polled = asyncio.Event()

    async def get_updates(self: Any, offset: int) -> list[Any]:
        polled.set()
        await asyncio.sleep(3600)
        return []

    monkeypatch.setattr(telegram_module.BotApi, "get_updates", get_updates)
    built: list[Any] = []
    monkeypatch.setattr(serve_cli, "build_session", _fake_session(built))

    code = await asyncio.wait_for(serve_cli._serve(get_settings(), lambda _line: None, until=polled.wait()), 10)

    assert code == 0
    (args, human_input), = built
    assert args.interactive is True and human_input is not None


# -- the worker pool ------------------------------------------------------------------------


def test_each_worker_has_its_own_profile_port_and_label(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.cli.serve import worker_args
    from src.config import ServeSettings, StorageSettings
    from src.serve.paths import serve_paths

    serve = ServeSettings(workers=3, cdp_port_base=9400)
    args = worker_args(serve, serve_paths(serve, StorageSettings(root_dir=Path("C:/stand"))), 2)

    assert Path(args.user_data_dir) == Path("C:/stand/serve/profiles/w2")
    assert args.cdp_port == 9402
    assert args.labels == {"mode": "serve", "worker": "w2"}


@pytest.mark.asyncio
async def test_the_stand_starts_one_session_per_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.cli import serve as serve_cli

    monkeypatch.setenv("AUTOBROWSER_SERVE__WORKERS", "2")
    monkeypatch.setenv("AUTOBROWSER_SERVE__API_APPROVALS", "true")
    get_settings.cache_clear()
    settings = get_settings()
    built: list[Any] = []
    monkeypatch.setattr(serve_cli, "build_session", _fake_session(built))

    code = await serve_cli._serve(settings, lambda _line: None, until=asyncio.sleep(0))

    assert code == 0
    assert [args.labels["worker"] for args, _ in built] == ["w0", "w1"]
    assert len({args.cdp_port for args, _ in built}) == 2
    assert len({args.user_data_dir for args, _ in built}) == 2
    brokers = [broker for _, broker in built]
    assert all(broker is not None for broker in brokers) and brokers[0] is not brokers[1]
    events = (settings.storage.root_dir / "serve" / "events.jsonl").read_text(encoding="utf-8")
    assert '"workers": 2' in events


@pytest.mark.asyncio
async def test_a_worker_that_does_not_start_refuses_the_stand_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.cli import serve as serve_cli
    from src.serve.pool import WorkerPool, WorkerStartError

    monkeypatch.setenv("AUTOBROWSER_SERVE__WORKERS", "2")
    get_settings.cache_clear()
    built: list[Any] = []
    monkeypatch.setattr(serve_cli, "build_session", _fake_session(built))

    async def broken(self: Any) -> None:
        raise WorkerStartError("worker w1 did not start: RuntimeError")

    monkeypatch.setattr(WorkerPool, "start", broken)

    code = await serve_cli._serve(get_settings(), lambda _line: None, until=asyncio.sleep(0))

    assert code == 2
    assert "worker w1 did not start" in capsys.readouterr().err


def test_the_summary_counts_the_workers(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AUTOBROWSER_SERVE__WORKERS", "3")
    get_settings.cache_clear()

    assert entry.main(["serve", "--check"]) == 0

    assert "3 workers" in capsys.readouterr().out


# -- doctor ---------------------------------------------------------------------------------


def _line(out: str, label: str) -> str:
    return next(line for line in out.splitlines() if line[4:].startswith(f"{label} "))


def test_doctor_checks_the_bot_has_no_webhook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.cli import subcommands

    with_telegram(monkeypatch)
    calls = webhook(monkeypatch)
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    entry.main(["doctor"])

    out = capsys.readouterr().out
    assert calls == [1]
    assert "token set" in _line(out, "telegram") and "1 chats" in _line(out, "telegram")
    assert _line(out, "telegram api").startswith("ok") and "no webhook" in out
    assert TOKEN not in out


def test_doctor_fails_a_bot_with_a_webhook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.cli import subcommands

    with_telegram(monkeypatch)
    webhook(monkeypatch, is_set=True)
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    assert entry.main(["doctor"]) == 1

    assert _line(capsys.readouterr().out, "telegram api").startswith("xx")


def test_doctor_without_the_channel_calls_no_bot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = webhook(monkeypatch)

    entry.main(["doctor"])

    assert calls == []
    assert "telegram" not in capsys.readouterr().out


def test_doctor_reports_broken_settings_without_their_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with_telegram(monkeypatch, chats="[]")

    assert entry.main(["doctor"]) == 1

    out = capsys.readouterr().out
    assert "allowed_chat_ids" in out
    assert TOKEN not in out
