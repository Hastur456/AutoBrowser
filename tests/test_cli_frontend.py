from __future__ import annotations

import argparse

import pytest

from src.cli import bootstrap
from src.cli.parser import build_parser


def parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def test_front_end_flags_override_the_ui_settings_for_one_run() -> None:
    ui = bootstrap.ui_settings_from_args(parse("--plain", "--ascii", "--no-color", "--theme", "mono"))

    assert (ui.frontend, ui.glyphs, ui.color, ui.theme) == ("plain", "ascii", "none", "mono")
    assert bootstrap.ui_settings_from_args(parse("--classic")).frontend == "classic"


def test_classic_and_plain_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse("--classic", "--plain")


@pytest.mark.parametrize("argv, expected", [(("--classic",), "classic"), ((), "frontend")])
def test_run_agent_cli_dispatches_by_front_end(
    monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...], expected: str
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        bootstrap, "run_classic_cli", lambda _args, **_kw: calls.append("classic") or 0
    )
    monkeypatch.setattr(
        bootstrap, "run_frontend_cli", lambda _args, _ui, **_kw: calls.append("frontend") or 0
    )
    # Whatever the personal profile says, these runs pin the front end explicitly.
    args = parse(*argv)
    if not argv:
        monkeypatch.setattr(
            bootstrap,
            "ui_settings_from_args",
            lambda _args: bootstrap.get_settings().ui.model_copy(update={"frontend": "auto"}),
        )

    assert bootstrap.run_agent_cli(args) == 0
    assert calls == [expected]
