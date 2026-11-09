"""``src/serve`` is a client of ``SessionController``: it never reaches into the engine, the
browser layer, the CLI composition, the providers, the harness or the chat channels, and
nothing below it imports it back (``docs/development/2026-11-02-serve-mode-design.md`` N2).

``src/channels`` holds the vendor adapters of chat channels: it may use only the stand's
neutral ports, the settings and the contracts, and only the CLI composition imports it
(``docs/development/2026-11-08-serve-mode-phase4-design.md``)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "src" / "serve"
CHANNELS = ROOT / "src" / "channels"

#: What the stand may not import.
FORBIDDEN_IN_SERVE = (
    "src.agent_loop.execution",
    "src.browser",
    "src.cli",
    "src.providers",
    "src.harness",
    "src.channels",
)
#: What a chat channel adapter may import from the project (plus third-party and stdlib).
ALLOWED_IN_CHANNELS = (
    "src.config",
    "src.contracts",
    "src.serve.commands",
    "src.serve.notify",
    "src.serve.clock",
    "src.channels",
)
#: The vendor host of the Telegram Bot API lives in its adapter only.
VENDOR_HOSTS = re.compile(r"api\.telegram\.org", re.IGNORECASE)
#: The stand and its channels run on one asyncio loop: no threads (file writes stay
#: race-free because every write is synchronous between two awaits).
THREAD_MARKERS = re.compile(r"\bto_thread\b|\brun_in_executor\b|\bimport threading\b|\bfrom threading\b")
#: Layers that may never import the stand.
LOWER_LAYERS = ("agent_loop", "harness", "ui", "browser", "mcp", "providers")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def test_the_serve_package_exists() -> None:
    assert (SERVE / "__init__.py").is_file()


@pytest.mark.parametrize("path", _python_files(SERVE), ids=lambda path: path.name)
def test_the_stand_imports_only_the_controller_side(path: Path) -> None:
    for name in _imports(path):
        for forbidden in FORBIDDEN_IN_SERVE:
            assert not (name == forbidden or name.startswith(f"{forbidden}.")), (
                f"{path.name} imports {name}"
            )


@pytest.mark.parametrize(
    "path",
    [path for layer in LOWER_LAYERS for path in _python_files(ROOT / "src" / layer)],
    ids=lambda path: f"{path.parent.name}/{path.name}",
)
def test_no_lower_layer_imports_the_stand(path: Path) -> None:
    for name in _imports(path):
        assert not (name == "src.serve" or name.startswith("src.serve.")), f"{path} imports {name}"


def test_the_channels_package_exists() -> None:
    assert (CHANNELS / "__init__.py").is_file()


@pytest.mark.parametrize("path", _python_files(CHANNELS), ids=lambda path: path.name)
def test_a_channel_imports_only_the_neutral_ports(path: Path) -> None:
    for name in _imports(path):
        if not name.startswith("src.") and name != "src":
            continue
        assert any(name == allowed or name.startswith(f"{allowed}.") for allowed in ALLOWED_IN_CHANNELS), (
            f"{path.name} imports {name}"
        )


@pytest.mark.parametrize(
    "path",
    [path for path in _python_files(ROOT / "src") if CHANNELS not in path.parents and path.parent.name != "cli"],
    ids=lambda path: path.relative_to(ROOT / "src").as_posix(),
)
def test_only_the_cli_imports_the_channels(path: Path) -> None:
    for name in _imports(path):
        assert not (name == "src.channels" or name.startswith("src.channels.")), f"{path} imports {name}"


@pytest.mark.parametrize(
    "path",
    [path for path in _python_files(ROOT / "src") if path != CHANNELS / "telegram.py"],
    ids=lambda path: path.relative_to(ROOT / "src").as_posix(),
)
def test_the_vendor_host_lives_in_its_adapter_only(path: Path) -> None:
    assert not VENDOR_HOSTS.search(path.read_text(encoding="utf-8")), f"{path} names the vendor host"


@pytest.mark.parametrize(
    "path", [*_python_files(SERVE), *_python_files(CHANNELS)], ids=lambda path: path.as_posix()
)
def test_the_stand_and_its_channels_use_no_threads(path: Path) -> None:
    assert not THREAD_MARKERS.search(path.read_text(encoding="utf-8")), f"{path} uses threads"
