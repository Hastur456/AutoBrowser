"""Pure rendering of transcript blocks, the live zone, the banner and the status line.

Every function here depends only on its arguments — ``(block, width, caps, theme, settings)``
plus an explicit ``now`` for timers — and returns a string: ANSI when the terminal supports
it, plain text otherwise. Nothing writes to a terminal (§6.2, rule 7), so the output is
snapshot-testable.

Layout rules from §5.7 and §6.1: a left gutter instead of boxes (no right borders), a
two-column content indent, nothing drawn in the last terminal column (B3), single-line chrome
cut by cells with the theme's ellipsis (B4), and only sanitized text from pages and tools.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from rich.cells import cell_len
from rich.console import Console, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from src.config import UiSettings
from src.ui.tui.strings import Strings
from src.ui.tui.terminal import TerminalCaps
from src.ui.tui.theme import ASCII_GLYPHS, Theme
from src.ui.tui.transcript import Block, TaskProgress, Transcript, approval_choices
from src.ui.views import FeedItem, SessionView

INDENT = "  "

#: Spaces at a line end, before any trailing SGR codes: rich pads some renderables.
_TRAILING_SPACES = re.compile(r" +((?:\x1b\[[0-9;]*m)*)$", re.MULTILINE)

_COLOR_SYSTEMS = {"none": "standard", "16": "standard", "256": "256", "truecolor": "truecolor"}


@dataclass(frozen=True)
class RenderContext:
    """Everything a render needs besides the thing being drawn."""

    width: int
    caps: TerminalCaps
    theme: Theme
    settings: UiSettings

    @property
    def usable(self) -> int:
        """Columns we may draw in: never the last one, so autowrap never triggers (B3)."""

        return max(self.width - 1, 10)

    @property
    def strings(self) -> Strings:
        return Strings(self.settings.locale, ascii_only=self.theme.glyphs is ASCII_GLYPHS)


# ---------------------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------------------


def render_block(
    block: Block,
    *,
    width: int,
    caps: TerminalCaps,
    theme: Theme,
    settings: UiSettings,
    now: datetime | None = None,
) -> str:
    """One block as terminal text, without a trailing newline."""

    ctx = RenderContext(width=width, caps=caps, theme=theme, settings=settings)
    return _to_text(ctx, _block_renderables(ctx, block, now))


def render_live(
    transcript: Transcript,
    *,
    width: int,
    caps: TerminalCaps,
    theme: Theme,
    settings: UiSettings,
    now: datetime,
    turn_cap: int | None = None,
) -> str:
    """The live zone: live blocks and the spinner line, at most ``ui.live_max_lines`` high.

    When it does not fit, the oldest lines go first and one ``… N more lines`` line says so.
    """

    ctx = RenderContext(width=width, caps=caps, theme=theme, settings=settings)
    parts: list[str] = []
    for block in transcript.live:
        if block.kind == "plan" and not transcript.show_plan:
            continue
        parts.append(_to_text(ctx, _block_renderables(ctx, block, now)))
    if transcript.task is not None:
        parts.append(_to_text(ctx, [spinner_line(ctx, transcript.task, now, turn_cap)]))
    lines = "\n".join(part for part in parts if part).split("\n") if parts else []
    limit = settings.live_max_lines
    if len(lines) > limit:
        hidden = len(lines) - limit + 1
        more = Text(f"{INDENT}{ctx.theme.glyphs.ellipsis} ", style=theme.style("muted"))
        more.append(ctx.strings("more_lines", n=hidden), style=theme.style("muted"))
        lines = [_to_text(ctx, [more]), *lines[hidden:]]
    return "\n".join(lines)


def _block_renderables(
    ctx: RenderContext, block: Block, now: datetime | None
) -> list[RenderableType]:
    if block.kind == "user":
        return [_user(ctx, block)]
    if block.kind == "plan":
        return _plan(ctx, block)
    if block.kind == "call":
        return _call(ctx, block)
    if block.kind == "approval":
        return _approval(ctx, block, now)
    if block.kind == "outcome":
        return _outcome(ctx, block)
    if block.kind == "command_output":
        return _command_output(ctx, block)
    return [_notice(ctx, block)]


def _user(ctx: RenderContext, block: Block) -> RenderableType:
    return _hanging(
        Text(f"{ctx.theme.glyphs.caret} ", style=ctx.theme.style("accent")),
        Text(block.text, style=ctx.theme.style("text")),
    )


def _hanging(prefix: Text, body: Text) -> Table:
    """``prefix`` then ``body``, wrapped lines aligned under the body (a hanging indent)."""

    grid = Table.grid(padding=0)
    grid.add_column(width=prefix.cell_len, no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row(prefix, body)
    return grid


def _plan(ctx: RenderContext, block: Block) -> list[RenderableType]:
    glyphs, theme = ctx.theme.glyphs, ctx.theme
    rows: list[RenderableType] = [Text(f"{INDENT}{ctx.strings('plan')}", style=theme.style("plan"))]
    current = block.current_step if block.current_step is not None else 0
    for index, step in enumerate(block.steps):
        status = block.step_statuses[index] if index < len(block.step_statuses) else ""
        if not status:
            # A plan without statuses: before the current step is done, it is active.
            status = "completed" if index < current else "in_progress" if index == current else ""
        if status == "completed":
            mark, style = glyphs.done, theme.style("ok")
            body = theme.style("muted")
        elif status == "in_progress":
            mark, style = glyphs.active, theme.style("accent")
            body = theme.style("plan")
        else:
            mark, style = glyphs.pending, theme.style("muted")
            body = theme.style("text")
        row = Text(INDENT)
        row.append(mark, style=style)
        row.append(" ")
        row.append(step, style=body)
        rows.append(row)
    return rows


def _notice(ctx: RenderContext, block: Block) -> RenderableType:
    role = {"info": "muted", "warn": "warn", "error": "error"}[block.level]
    message = ctx.strings("gap", n=block.missed) if block.missed else block.text
    return _hanging(Text(INDENT), Text(message, style=ctx.theme.style(role)))


def _command_output(ctx: RenderContext, block: Block) -> list[RenderableType]:
    rows: list[RenderableType] = []
    if block.text:
        rows.append(Text(f"{INDENT}{block.text}", style=ctx.theme.style("plan")))
    rows.extend(Text(_fit(f"{INDENT}{line}", ctx.usable, ctx)) for line in block.lines)
    return rows


# ---------------------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------------------


def _call(ctx: RenderContext, block: Block) -> list[RenderableType]:
    proposed = _first(block.items, "call")
    finished = _first(block.items, "tool_finished")
    started = _first(block.items, "tool_started")
    tool = (proposed.tool if proposed else "") or (finished.tool if finished else "")
    server = next((item.server for item in block.items if item.server), "")
    args = proposed.args if proposed else {}

    header = Text()
    header.append(ctx.theme.glyphs.call, style=ctx.theme.style("tool"))
    header.append(" ")
    if server:
        header.append(server, style=ctx.theme.style("muted"))
        header.append(" · ", style=ctx.theme.style("muted"))
    header.append(tool or "?", style=ctx.theme.style("tool"))
    rows: list[RenderableType] = []
    if not block.expanded and args:
        preview = _args_inline(args, ctx)
        room = ctx.usable - header.cell_len - 2
        if room >= 12:
            header.append("  ")
            header.append(_fit(preview, room, ctx), style=ctx.theme.style("text"))
            rows.append(header)
        else:
            rows.append(_fitted(header, ctx))
            rows.append(Text(_fit(f"{INDENT}{preview}", ctx.usable, ctx)))
    else:
        rows.append(_fitted(header, ctx))

    if block.expanded:
        rows.extend(_call_details(ctx, block, args))

    for item in block.items:
        line = _gate_line(ctx, item)
        if line is not None:
            rows.append(line)

    if finished is not None:
        rows.extend(_result_rows(ctx, block, finished, started))
    elif block.state == "live" and not _blocked(block):
        rows.append(_elbow(ctx, Text(ctx.theme.glyphs.ellipsis, style=ctx.theme.style("muted"))))
    return rows


def _call_details(ctx: RenderContext, block: Block, args: Mapping[str, object]) -> list[Text]:
    pipe = ctx.theme.glyphs.pipe
    rows: list[Text] = []
    if args:
        line = Text(f"{INDENT}{pipe} ", style=ctx.theme.style("muted"))
        line.append(f"{ctx.strings('args'):<7}", style=ctx.theme.style("muted"))
        line.append(_fit(_json(args), ctx.usable - line.cell_len, ctx))
        rows.append(line)
    permission = next(
        (item for item in block.items if item.kind == "gate" and item.origin == "permission"),
        None,
    )
    if permission is not None:
        line = Text(f"{INDENT}{pipe} ", style=ctx.theme.style("muted"))
        line.append(f"{ctx.strings('access'):<7}", style=ctx.theme.style("muted"))
        detail = permission.decision
        if permission.rule:
            detail = f"{detail} · {permission.rule}"
        line.append(_fit(detail, ctx.usable - line.cell_len, ctx), style=_decision_style(ctx, permission.decision))
        rows.append(line)
    return rows


def _gate_line(ctx: RenderContext, item: FeedItem) -> Text | None:
    if item.kind == "gate":
        if item.decision == "allow":
            return None
        glyph = "?" if item.decision == "ask" else ctx.theme.glyphs.skip
        if item.decision == "error":
            glyph = ctx.theme.glyphs.fail
        label = ctx.strings(item.origin) if item.origin in {"hook", "permission", "guard"} else item.origin
        head = f"{label} {item.rule}".strip()
        body = f"{head}: {item.text}" if item.text else f"{head} {item.decision}".strip()
        return _elbow(ctx, _status_text(ctx, glyph, body, _decision_style(ctx, item.decision)))
    if item.kind == "approval_requested":
        body = ctx.strings("approval_title")
        if item.rule:
            body = f"{body} · {ctx.strings('rule')} {item.rule}"
        return _elbow(ctx, _status_text(ctx, "?", body, ctx.theme.style("warn")))
    if item.kind == "approval_resolved" and item.origin in {"grant", "hook"}:
        body = f"{item.decision} · {item.origin}"
        if item.status:
            body = f"{body} ({item.status})"
        return _elbow(ctx, Text(body, style=ctx.theme.style("muted")))
    return None


def _result_rows(
    ctx: RenderContext, block: Block, finished: FeedItem, started: FeedItem | None
) -> list[Text]:
    ok = finished.status == "success"
    glyph = ctx.theme.glyphs.ok if ok else ctx.theme.glyphs.fail
    style = ctx.theme.style("ok" if ok else "error")
    duration = _duration(started, finished)
    head = Text(glyph, style=style)
    if block.expanded:
        head.append(f" {finished.status or '?'}", style=style)
        if duration:
            head.append(f" · {duration}", style=ctx.theme.style("muted"))
    else:
        if duration:
            head.append(f" {duration}", style=ctx.theme.style("muted"))
        summary = finished.url if ok and finished.url else _first_line(finished.text)
        if summary:
            head.append("  ")
            room = ctx.usable - cell_len(INDENT) - 2 - head.cell_len
            head.append(_fit(summary, room, ctx), style=ctx.theme.style("link" if finished.url and ok else "muted" if ok else "error"))
    rows = [_elbow(ctx, head)]
    if block.expanded:
        rows.extend(_content_preview(ctx, block, finished.text))
    return rows


def _content_preview(ctx: RenderContext, block: Block, content: str) -> list[Text]:
    lines = [line for line in content.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    shown = lines[: ctx.settings.preview_lines]
    rows = [
        Text(_fit(f"{INDENT}{INDENT}{line}", ctx.usable, ctx), style=ctx.theme.style("muted"))
        for line in shown
    ]
    extra: list[str] = []
    if len(lines) > len(shown):
        extra.append(ctx.strings("more_lines", n=len(lines) - len(shown)))
    observation = _first(block.items, "observation")
    if observation is not None:
        extra.append(ctx.strings("observation", n=_compact_count(len(observation.text))))
        if observation.status == "snapshot":
            extra.append(ctx.strings("snapshot"))
    if extra:
        rows.append(
            Text(
                _fit(f"{INDENT}{INDENT}{ctx.theme.glyphs.ellipsis} " + " · ".join(extra), ctx.usable, ctx),
                style=ctx.theme.style("muted"),
            )
        )
    return rows


def _blocked(block: Block) -> bool:
    return any(
        item.kind == "gate" and item.decision in {"deny", "blocked"} for item in block.items
    )


# ---------------------------------------------------------------------------------------
# Approval card
# ---------------------------------------------------------------------------------------


def _approval(ctx: RenderContext, block: Block, now: datetime | None) -> list[RenderableType]:
    ticket = block.ticket
    if ticket is None:
        return []
    if block.state == "frozen" or block.resolution is not None:
        return [_approval_summary(ctx, block)]

    theme, glyphs, s = ctx.theme, ctx.theme.glyphs, ctx.strings
    bar = Text(f"{glyphs.bar} ", style=theme.style("warn"))
    rows: list[RenderableType] = []

    def row(*parts: tuple[str, str]) -> None:
        line = bar.copy()
        for text, role in parts:
            line.append(text, style=theme.style(role))
        rows.append(_fitted(line, ctx))

    title = s("approval_title")
    if ticket.rule_id:
        title = f"{title} · {s('rule')} {ticket.rule_id}"
    head = bar.copy()
    head.append(title, style=theme.style("warn"))
    if now is not None:
        remaining = max(int((ticket.deadline - now).total_seconds()), 0)
        timer = _clock(remaining)
        pad = ctx.usable - head.cell_len - cell_len(timer)
        if pad >= 2:
            head.append(" " * pad)
            head.append(timer, style=theme.style("muted"))
    rows.append(_fitted(head, ctx))
    target = f"{ticket.server} · {ticket.tool}" if ticket.server else ticket.tool
    if ticket.domain:
        row((target, "tool"), (f"  {s('on_domain')} ", "muted"), (ticket.domain, "link"))
    else:
        row((target, "tool"))
    if ticket.args_preview:
        row((ticket.args_preview, "text"))
    if ticket.reason:
        row((f"{s('reason')}: ", "muted"), (ticket.reason, "text"))
    rows.append(bar.copy())
    for index, choice in enumerate(approval_choices(block)):
        selected = index == block.choice
        line = bar.copy()
        line.append(f"{glyphs.pointer if selected else ' '} ", style=theme.style("accent"))
        label = f"{index + 1}. {_choice_label(ctx, choice, ticket.tool, ticket.domain)}"
        line.append(label, style=theme.selected() if selected else theme.style("text"))
        rows.append(_fitted(line, ctx))
    rows.append(bar.copy())
    row((s("approval_hint"), "muted"))
    return rows


def _choice_label(ctx: RenderContext, choice: str, tool: str, domain: str) -> str:
    if choice == "once":
        return ctx.strings("approve_once")
    if choice == "session":
        if domain:
            return ctx.strings("approve_session", tool=tool, domain=domain)
        return ctx.strings("approve_session_any", tool=tool)
    return ctx.strings("deny")


def _approval_summary(ctx: RenderContext, block: Block) -> Text:
    resolution, ticket = block.resolution, block.ticket
    answer = resolution.answer if resolution is not None else "deny"
    via = resolution.via if resolution is not None else ""
    if via == "timeout":
        key, glyph, role = "answered_timeout", ctx.theme.glyphs.fail, "error"
    elif answer == "deny":
        key, glyph, role = "answered_deny", ctx.theme.glyphs.fail, "error"
    else:
        key = "answered_session" if answer == "session" else "answered_once"
        glyph, role = ctx.theme.glyphs.ok, "ok"
    body = Text(f"{glyph} ", style=ctx.theme.style(role))
    body.append(ctx.strings(key), style=ctx.theme.style(role))
    if via and via != "timeout":
        body.append(f" ({via})", style=ctx.theme.style("muted"))
    if resolution is not None and ticket is not None:
        seconds = max((resolution.resolved_at - ticket.created_at).total_seconds(), 0.0)
        body.append(f" · {seconds:.1f}s", style=ctx.theme.style("muted"))
    return _elbow(ctx, body)


# ---------------------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------------------


def _outcome(ctx: RenderContext, block: Block) -> list[RenderableType]:
    item = block.items[0] if block.items else None
    if item is None:
        return []
    theme, glyphs, s = ctx.theme, ctx.theme.glyphs, ctx.strings
    status = item.status
    if status == "completed":
        glyph, role, title = glyphs.ok, "ok", s("done")
    elif status == "cancelled":
        glyph, role, title = glyphs.skip, "warn", s("cancelled")
    elif status == "blocked":
        glyph, role, title = glyphs.fail, "error", s("blocked")
    else:
        glyph, role, title = glyphs.fail, "error", s("failed")

    head = Text(f"{glyph} {title}", style=theme.style(role))
    facts: list[str] = []
    if status == "blocked":
        facts.append("blocked")
    if item.turns is not None:
        facts.append(s.count("turns", item.turns))
    if block.calls and status == "completed":
        facts.append(s.count("calls", block.calls))
    if block.elapsed_seconds is not None:
        facts.append(_clock(int(block.elapsed_seconds), pad=False))
    if status == "failed" and item.text:
        facts.append(_first_line(item.text))
    if facts:
        head.append(" · " + " · ".join(facts), style=theme.style("muted"))
    rows: list[RenderableType] = [head]

    if status == "completed" and item.text:
        rows.append(Text(""))
        body: RenderableType
        if ctx.settings.markdown:
            body = Markdown(item.text, hyperlinks=ctx.caps.hyperlinks)
        else:
            body = Text(item.text)
        rows.append(Padding(body, (0, 0, 0, 2)))
    elif status in {"blocked", "cancelled"} and item.text:
        rows.append(_elbow(ctx, Text(_first_line(item.text), style=theme.style("muted"))))
    return rows


# ---------------------------------------------------------------------------------------
# Spinner, status line, banner
# ---------------------------------------------------------------------------------------


def spinner_line(
    ctx: RenderContext, task: TaskProgress, now: datetime, turn_cap: int | None
) -> Text:
    """``⠼ Думаю… · ход 4/50 · 00:37 · 3 вызова · esc — прервать`` cut to the width.

    Parts are dropped from the end when the line does not fit; numbers keep fixed widths so
    the line does not jitter (B12).
    """

    frames = ctx.theme.glyphs.spinner
    elapsed = _elapsed(task.started_at, now)
    tick = int(elapsed * ctx.settings.refresh_hz)
    frame = frames[tick % len(frames)]
    phase = {"plan": "planning", "approval": "judging", "tool": "running"}.get(task.phase, "thinking")
    parts = [ctx.strings(phase)]
    if task.turn is not None:
        cap = f"/{turn_cap}" if turn_cap else ""
        parts.append(f"{ctx.strings('turn')} {task.turn}{cap}")
    parts.append(_clock(int(elapsed)))
    if task.calls:
        parts.append(ctx.strings.count("calls", task.calls))
    parts.append(ctx.strings("esc_cancel"))
    line = Text(f"{frame} ", style=ctx.theme.style("accent"))
    return _drop_to_fit(ctx, line, parts, first_role="text")


def render_status(
    view: SessionView,
    transcript: Transcript,
    *,
    width: int,
    caps: TerminalCaps,
    theme: Theme,
    settings: UiSettings,
    now: datetime,
) -> str:
    """The bottom status line; segments drop by priority when the terminal is narrow."""

    ctx = RenderContext(width=width, caps=caps, theme=theme, settings=settings)
    s = ctx.strings
    task = transcript.task
    browser = _browser_segment(ctx, view)
    turn = ""
    if task is not None:
        cap = f"/{view.turn_cap}" if view.turn_cap else ""
        turn = f"{s('turn')} {task.turn or 0}{cap} · {_clock(int(_elapsed(task.started_at, now)))}"
    segments = {
        "model": view.model or "?",
        "session": view.session_id[:4],
        "browser": browser,
        "permissions": view.permission_mode,
        "state": turn or s("ready"),
    }
    order = ("model", "session", "browser", "permissions", "state")
    help_text = s("help")

    def line(names: set[str], with_help: bool) -> Text | None:
        left = INDENT + " · ".join(segments[n] for n in order if n in names and segments[n])
        if not with_help:
            return Text(left, style=theme.style("muted")) if cell_len(left) <= ctx.usable else None
        gap = ctx.usable - cell_len(left) - cell_len(help_text)
        return Text(left + " " * gap + help_text, style=theme.style("muted")) if gap >= 2 else None

    # Kept by priority turn > browser > model > session > permissions (§5.7.4): the help
    # hint goes first, then permissions and session, then the model is shortened, then
    # the browser goes.
    names = set(order)
    for with_help, drop in ((True, ""), (False, ""), (False, "permissions"), (False, "session")):
        names.discard(drop)
        fitted = line(names, with_help)
        if fitted is not None:
            return _to_text(ctx, [fitted])
    model = segments["model"]
    for drop in ("", "browser"):
        names.discard(drop)
        others = INDENT + " · ".join(
            segments[n] for n in order if n in names - {"model"} and segments[n]
        )
        room = ctx.usable - cell_len(others) - cell_len(" · ")
        if room > cell_len(ctx.theme.glyphs.ellipsis):
            segments["model"] = _fit(model, room, ctx)
            fitted = line(names, False)
            if fitted is not None:
                return _to_text(ctx, [fitted])
    left = INDENT + " · ".join(segments[n] for n in ("model", "state") if segments[n])
    return _to_text(ctx, [Text(_fit(left, ctx.usable, ctx), style=theme.style("muted"))])


def render_banner(
    view: SessionView,
    *,
    width: int,
    caps: TerminalCaps,
    theme: Theme,
    settings: UiSettings,
) -> str:
    """The welcome banner: the browser-window icon and the session facts beside it."""

    ctx = RenderContext(width=width, caps=caps, theme=theme, settings=settings)
    s, glyphs = ctx.strings, theme.glyphs
    title = f"AutoBrowser {view.version}".rstrip()
    model = " · ".join(part for part in (view.model, view.provider) if part)
    session = " · ".join(part for part in (view.session_id[:8], view.workspace) if part)
    permissions = " · ".join(
        (
            view.permission_mode or "default",
            s("hooks_on") if view.hooks_enabled else s("hooks_off"),
            s("memory_on") if view.memory_enabled else s("memory_off"),
        )
    )
    facts = [
        Text(title, style=theme.style("plan")),
        _fact(ctx, s("model"), model),
        _fact(ctx, s("session"), session),
        _fact(ctx, s("browser"), _browser_segment(ctx, view)),
        _fact(ctx, s("permissions"), permissions),
    ]
    rows: list[RenderableType] = []
    icon = glyphs.icon
    icon_width = cell_len(icon[0])
    side_by_side = ctx.usable >= icon_width + 2 + 30
    if side_by_side:
        room = ctx.usable - cell_len(INDENT) - icon_width - 2
        for line, fact in zip(icon, facts):
            row = Text(INDENT)
            row.append(line, style=theme.style("accent"))
            row.append("  ")
            row.append_text(_cut(fact, room, ctx))
            rows.append(row)
    else:
        rows.extend(_fitted(Text(INDENT) + fact, ctx) for fact in facts)
    rows.append(Text(""))
    rows.append(Text(_fit(f"{INDENT}{s('banner_hint')}", ctx.usable, ctx), style=theme.style("muted")))
    return _to_text(ctx, rows)


def render_rule(*, width: int, caps: TerminalCaps, theme: Theme, settings: UiSettings) -> str:
    """The horizontal line above and below the input field."""

    ctx = RenderContext(width=width, caps=caps, theme=theme, settings=settings)
    return _to_text(ctx, [Text(theme.glyphs.rule * ctx.usable, style=theme.style("border"))])


def _fact(ctx: RenderContext, label: str, value: str) -> Text:
    text = Text(f"{label:<10} ", style=ctx.theme.style("muted"))
    text.append(value or "-", style=ctx.theme.style("text"))
    return text


def _browser_segment(ctx: RenderContext, view: SessionView) -> str:
    if not view.servers:
        return view.browser or ctx.strings("browser_off")
    parts = []
    for server in view.servers:
        mark = ctx.theme.glyphs.call if server.state == "ready" else ctx.theme.glyphs.fail
        parts.append(f"{mark} {server.name}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def _to_text(ctx: RenderContext, renderables: Iterable[RenderableType]) -> str:
    """Render to a string at the usable width, ANSI only above the plain level."""

    buffer = io.StringIO()
    plain = ctx.caps.level == "plain"
    console = Console(
        file=buffer,
        width=ctx.usable,
        color_system=None if plain else _COLOR_SYSTEMS[ctx.theme.color],  # type: ignore[arg-type]
        force_terminal=not plain,
        no_color=ctx.theme.color == "none",
        legacy_windows=False,
        theme=ctx.theme.rich_theme(),
        emoji=False,
        highlight=False,
        markup=False,
        _environ={},
    )
    for renderable in renderables:
        console.print(renderable, overflow="fold", crop=True)
    return _TRAILING_SPACES.sub(r"\1", buffer.getvalue()).rstrip("\n")


def _elbow(ctx: RenderContext, body: Text) -> Text:
    line = Text(f"{INDENT}{ctx.theme.glyphs.elbow} ", style=ctx.theme.style("muted"))
    line.append_text(body)
    return _fitted(line, ctx)


def _status_text(ctx: RenderContext, glyph: str, body: str, style: object) -> Text:
    text = Text(f"{glyph} ", style=style)  # type: ignore[arg-type]
    text.append(body, style=style)  # type: ignore[arg-type]
    return text


def _decision_style(ctx: RenderContext, decision: str) -> object:
    if decision in {"deny", "blocked", "error"}:
        return ctx.theme.style("error")
    if decision == "ask":
        return ctx.theme.style("warn")
    return ctx.theme.style("muted")


def _fitted(text: Text, ctx: RenderContext) -> Text:
    return _cut(text, ctx.usable, ctx)


def _cut(text: Text, cells: int, ctx: RenderContext) -> Text:
    """``text`` cut to ``cells`` cells, ending with the theme's ellipsis when cut."""

    if text.cell_len <= cells:
        return text
    ellipsis = ctx.theme.glyphs.ellipsis
    keep = max(cells - cell_len(ellipsis), 0)
    cut = text.copy()
    cut.truncate(keep)
    while cut.cell_len > keep:
        cut.right_crop(1)
    cut.append(ellipsis, style=ctx.theme.style("muted"))
    return cut


def _fit(value: str, cells: int, ctx: RenderContext) -> str:
    """A single line of ``value`` cut to ``cells`` cells with the theme's ellipsis."""

    line = value.replace("\n", " ").replace("\t", " ")
    if cell_len(line) <= cells:
        return line
    ellipsis = ctx.theme.glyphs.ellipsis
    budget = max(cells - cell_len(ellipsis), 0)
    out: list[str] = []
    used = 0
    for char in line:
        width = cell_len(char)
        if used + width > budget:
            break
        out.append(char)
        used += width
    return "".join(out) + ellipsis


def _args_inline(args: Mapping[str, object], ctx: RenderContext) -> str:
    """``url: https://x  element: "Поиск"`` within ``ui.preview_chars`` characters."""

    parts = []
    for key, value in args.items():
        if isinstance(value, str):
            shown = f'"{value}"' if (" " in value or not value) else value
        else:
            shown = _json(value)
        parts.append(f"{key}: {shown}")
    text = "  ".join(parts)
    limit = ctx.settings.preview_chars
    return text if len(text) <= limit else text[:limit] + ctx.theme.glyphs.ellipsis


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _first(items: Iterable[FeedItem], kind: str) -> FeedItem | None:
    return next((item for item in items if item.kind == kind), None)


def _first_line(text: str) -> str:
    for line in text.split("\n"):
        if line.strip():
            return line.strip()
    return ""


def _duration(started: FeedItem | None, finished: FeedItem) -> str:
    if started is None or started.timestamp is None or finished.timestamp is None:
        return ""
    seconds = max((finished.timestamp - started.timestamp).total_seconds(), 0.0)
    return f"{seconds:.1f}s"


def _elapsed(started_at: datetime | None, now: datetime) -> float:
    if started_at is None:
        return 0.0
    return max((now - started_at).total_seconds(), 0.0)


def _clock(seconds: int, *, pad: bool = True) -> str:
    """``00:37`` (padded, for live timers) or ``1:12`` (for summaries)."""

    minutes, secs = divmod(max(seconds, 0), 60)
    return f"{minutes:02d}:{secs:02d}" if pad else f"{minutes}:{secs:02d}"


def _compact_count(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _drop_to_fit(ctx: RenderContext, line: Text, parts: list[str], *, first_role: str) -> Text:
    """Append ``parts`` joined by `` · ``, dropping trailing parts until the line fits."""

    keep = list(parts)
    while keep:
        candidate = line.copy()
        candidate.append(keep[0], style=ctx.theme.style(first_role))
        if len(keep) > 1:
            candidate.append(" · " + " · ".join(keep[1:]), style=ctx.theme.style("muted"))
        if candidate.cell_len <= ctx.usable or len(keep) == 1:
            return _fitted(candidate, ctx)
        keep.pop()
    return _fitted(line, ctx)


__all__ = [
    "RenderContext",
    "render_banner",
    "render_block",
    "render_live",
    "render_rule",
    "render_status",
    "spinner_line",
]
