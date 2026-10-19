"""Palette, glyphs and styles of the terminal UI (§5.7 of the terminal UI design).

Render code knows *roles* (``accent``, ``tool``, ``warn``...), never colours. One
:class:`Theme` turns the roles into a rich theme for the frozen blocks and a prompt_toolkit
style for the live zone, at the colour depth the terminal supports; truecolor is never
emitted unconditionally. Every glyph of :data:`UNICODE_GLYPHS` is one cell wide in both rich
and wcwidth (checked by ``tests/ui/test_theme_glyphs.py``); :data:`ASCII_GLYPHS` replaces it
where the font or console cannot draw it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from prompt_toolkit.styles import Style as PtStyle
from rich.style import Style
from rich.theme import Theme as RichTheme

from src.config import UiSettings
from src.ui.tui.terminal import ColorDepth, TerminalCaps

#: The colour roles of the palette, in display order.
ROLES = (
    "accent",
    "text",
    "muted",
    "tool",
    "link",
    "ok",
    "warn",
    "error",
    "plan",
    "border",
)

#: Roles drawn bold with colour, and without it (where bold carries the hierarchy).
_BOLD_ROLES = frozenset({"plan"})
_MONO_BOLD_ROLES = frozenset({"plan", "accent", "error", "warn", "tool"})

#: rich's 16-colour names → prompt_toolkit's ANSI colour names.
_PT_ANSI = {
    "default": "",
    "black": "ansiblack",
    "red": "ansired",
    "green": "ansigreen",
    "yellow": "ansiyellow",
    "blue": "ansiblue",
    "magenta": "ansimagenta",
    "cyan": "ansicyan",
    "white": "ansiwhite",
    "bright_black": "ansibrightblack",
    "bright_red": "ansibrightred",
    "bright_green": "ansibrightgreen",
    "bright_yellow": "ansibrightyellow",
    "bright_blue": "ansibrightblue",
    "bright_magenta": "ansibrightmagenta",
    "bright_cyan": "ansibrightcyan",
    "bright_white": "ansiwhite",
}


@dataclass(frozen=True)
class Swatch:
    """One colour at every depth: ``Swatch("#4FB3FF", 75, "bright_blue")``.

    An empty ``truecolor`` means the terminal's own foreground (the ``text`` role).
    """

    truecolor: str
    c256: int | None
    c16: str

    def rich_color(self, depth: ColorDepth) -> str | None:
        if depth == "none" or not self.truecolor:
            return None
        if depth == "truecolor":
            return self.truecolor
        if depth == "256" and self.c256 is not None:
            return f"color({self.c256})"
        return None if self.c16 == "default" else self.c16

    def pt_color(self, depth: ColorDepth) -> str:
        """prompt_toolkit maps a hex colour down to the output's depth by itself, so only
        the 16-colour case needs the ANSI name."""

        if depth == "none" or not self.truecolor:
            return ""
        if depth == "16":
            return _PT_ANSI.get(self.c16, "")
        return self.truecolor


@dataclass(frozen=True)
class Palette:
    accent: Swatch
    text: Swatch
    muted: Swatch
    tool: Swatch
    link: Swatch
    ok: Swatch
    warn: Swatch
    error: Swatch
    plan: Swatch
    border: Swatch

    def swatch(self, role: str) -> Swatch:
        return getattr(self, role)


DARK = Palette(
    accent=Swatch("#4FB3FF", 75, "bright_blue"),
    text=Swatch("", None, "default"),
    muted=Swatch("#8A8F98", 245, "bright_black"),
    tool=Swatch("#C792EA", 176, "magenta"),
    link=Swatch("#5FD7D7", 80, "cyan"),
    ok=Swatch("#7FD962", 113, "green"),
    warn=Swatch("#FFB454", 215, "yellow"),
    error=Swatch("#FF6B6B", 203, "red"),
    plan=Swatch("#E6E6E6", 254, "white"),
    border=Swatch("#3A3F4B", 238, "bright_black"),
)

LIGHT = Palette(
    accent=Swatch("#0A6ED1", 25, "blue"),
    text=Swatch("", None, "default"),
    muted=Swatch("#6B7280", 243, "bright_black"),
    tool=Swatch("#8E44AD", 97, "magenta"),
    link=Swatch("#00838F", 30, "cyan"),
    ok=Swatch("#2E8B3A", 28, "green"),
    warn=Swatch("#B26B00", 130, "yellow"),
    error=Swatch("#C62828", 160, "red"),
    plan=Swatch("#1F2328", 234, "black"),
    border=Swatch("#C9CED6", 251, "bright_black"),
)

PALETTES: dict[str, Palette] = {"dark": DARK, "light": LIGHT, "mono": DARK}


@dataclass(frozen=True)
class GlyphSet:
    call: str
    elbow: str
    ok: str
    fail: str
    skip: str
    pending: str
    active: str
    done: str
    bar: str
    #: The gutter of an expanded call's details.
    pipe: str
    pointer: str
    caret: str
    rule: str
    ellipsis: str
    spinner: tuple[str, ...]
    #: The banner icon, one string per line; all lines are the same width.
    icon: tuple[str, ...]

    def chrome(self) -> tuple[str, ...]:
        """Every single-cell glyph of the set (the icon lines are checked separately)."""

        values: list[str] = []
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "icon":
                continue
            if isinstance(value, tuple):
                values.extend(value)
            else:
                values.append(value)
        return tuple(values)


UNICODE_GLYPHS = GlyphSet(
    call="●",
    elbow="⎿",
    ok="✓",
    fail="✗",
    skip="⊘",
    pending="○",
    active="▸",
    done="✓",
    bar="▌",
    pipe="│",
    pointer="❯",
    caret="›",
    rule="─",
    ellipsis="…",
    spinner=("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
    icon=(
        "╭───────╮",
        "│ ● ● ● │",
        "├───────┤",
        "│  ›_   │",
        "╰───────╯",
    ),
)

ASCII_GLYPHS = GlyphSet(
    call="*",
    elbow="`",
    ok="ok",
    fail="x",
    skip="-",
    pending="[ ]",
    active="[>]",
    done="[x]",
    bar="|",
    pipe="|",
    pointer=">",
    caret=">",
    rule="-",
    ellipsis="...",
    spinner=("|", "/", "-", "\\"),
    icon=(
        "+-------+",
        "| o o o |",
        "+-------+",
        "|  >_   |",
        "+-------+",
    ),
)


@dataclass(frozen=True)
class Theme:
    """A palette and glyph set resolved for one terminal."""

    name: str
    palette: Palette
    glyphs: GlyphSet
    color: ColorDepth

    @classmethod
    def resolve(cls, settings: UiSettings, caps: TerminalCaps) -> Theme:
        color: ColorDepth = "none" if settings.theme == "mono" else caps.color
        if settings.glyphs == "ascii":
            glyphs = ASCII_GLYPHS
        elif settings.glyphs == "unicode":
            glyphs = UNICODE_GLYPHS
        else:
            glyphs = UNICODE_GLYPHS if caps.unicode else ASCII_GLYPHS
        return cls(
            name=settings.theme,
            palette=PALETTES[settings.theme],
            glyphs=glyphs,
            color=color,
        )

    def style(self, role: str) -> Style:
        """The rich style of a role; without colour, hierarchy is kept by attributes only.

        ``dim`` is never used: it is unreadable in the Windows 10 conhost.
        """

        color = self.palette.swatch(role).rich_color(self.color)
        bold_roles = _MONO_BOLD_ROLES if self.color == "none" else _BOLD_ROLES
        return Style(
            color=color,
            bold=True if role in bold_roles else None,
            underline=True if role == "link" else None,
        )

    def selected(self) -> Style:
        """The active choice of an approval card."""

        if self.color == "none":
            return Style(reverse=True)
        return Style(color=self.palette.accent.rich_color(self.color), bold=True)

    def rich_theme(self) -> RichTheme:
        styles = {f"ui.{role}": self.style(role) for role in ROLES}
        styles["ui.selected"] = self.selected()
        # Inherit rich's defaults: the markdown renderer of the final answer needs them.
        return RichTheme(styles, inherit=True)

    def pt_style(self) -> PtStyle:
        rules: list[tuple[str, str]] = []
        for role in ROLES:
            parts: list[str] = []
            color = self.palette.swatch(role).pt_color(self.color)
            if color:
                parts.append(f"fg:{color}")
            rich_style = self.style(role)
            if rich_style.bold:
                parts.append("bold")
            if rich_style.underline:
                parts.append("underline")
            rules.append((role, " ".join(parts)))
        selected = "reverse" if self.color == "none" else (
            f"fg:{self.palette.accent.pt_color(self.color)} bold".strip()
        )
        rules.append(("selected", selected))
        return PtStyle(rules)


__all__ = [
    "ASCII_GLYPHS",
    "DARK",
    "LIGHT",
    "PALETTES",
    "ROLES",
    "UNICODE_GLYPHS",
    "GlyphSet",
    "Palette",
    "Swatch",
    "Theme",
]
