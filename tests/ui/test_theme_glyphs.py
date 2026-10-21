from __future__ import annotations

import pytest
import wcwidth
from rich.cells import cell_len

from src.config import UiSettings
from src.ui.tui.terminal import TerminalCaps
from src.ui.tui.theme import ASCII_GLYPHS, PALETTES, ROLES, UNICODE_GLYPHS, Theme


def caps(color: str = "truecolor", unicode: bool = True) -> TerminalCaps:
    return TerminalCaps(
        level="full",
        color=color,  # type: ignore[arg-type]
        unicode=unicode,
        sync_output=False,
        hyperlinks=False,
        title=False,
        width=80,
        height=24,
        reason="test",
    )


@pytest.mark.parametrize("glyph", UNICODE_GLYPHS.chrome())
def test_every_unicode_chrome_glyph_is_one_cell_in_rich_and_wcwidth(glyph: str) -> None:
    assert cell_len(glyph) == 1
    assert wcwidth.wcswidth(glyph) == 1


@pytest.mark.parametrize("glyphs", [UNICODE_GLYPHS, ASCII_GLYPHS])
def test_banner_icon_lines_have_one_width(glyphs: object) -> None:
    widths = {(cell_len(line), wcwidth.wcswidth(line)) for line in glyphs.icon}  # type: ignore[attr-defined]

    assert len(widths) == 1
    width, wc = widths.pop()
    assert width == wc


def test_ascii_glyphs_are_ascii() -> None:
    for glyph in (*ASCII_GLYPHS.chrome(), *ASCII_GLYPHS.icon):
        assert glyph.isascii()


@pytest.mark.parametrize("name", ["dark", "light"])
def test_palettes_define_every_role_at_every_depth(name: str) -> None:
    palette = PALETTES[name]
    for role in ROLES:
        swatch = palette.swatch(role)
        if role == "text":
            assert swatch.rich_color("truecolor") is None
            continue
        assert swatch.rich_color("truecolor", ) == swatch.truecolor
        assert swatch.rich_color("256") == f"color({swatch.c256})"
        assert swatch.rich_color("16") == swatch.c16
        assert swatch.rich_color("none") is None


def test_theme_resolves_glyphs_and_colour_depth() -> None:
    assert Theme.resolve(UiSettings(), caps()).glyphs is UNICODE_GLYPHS
    assert Theme.resolve(UiSettings(), caps(unicode=False)).glyphs is ASCII_GLYPHS
    assert Theme.resolve(UiSettings(glyphs="ascii"), caps()).glyphs is ASCII_GLYPHS
    assert Theme.resolve(UiSettings(theme="light"), caps("256")).color == "256"
    assert Theme.resolve(UiSettings(theme="mono"), caps()).color == "none"


def test_mono_theme_keeps_hierarchy_with_attributes_only() -> None:
    theme = Theme.resolve(UiSettings(theme="mono"), caps())

    for role in ROLES:
        assert theme.style(role).color is None
        # dim is unreadable in the Windows 10 conhost
        assert not theme.style(role).dim
    assert theme.style("plan").bold and theme.style("error").bold
    assert theme.style("link").underline
    assert theme.selected().reverse


def test_rich_and_prompt_toolkit_styles_build_for_every_depth() -> None:
    for depth in ("none", "16", "256", "truecolor"):
        theme = Theme.resolve(UiSettings(), caps(depth))
        rich_theme = theme.rich_theme()
        assert "ui.accent" in rich_theme.styles
        theme.pt_style()
    sixteen = Theme.resolve(UiSettings(), caps("16"))
    assert sixteen.palette.accent.pt_color("16") == "ansibrightblue"
