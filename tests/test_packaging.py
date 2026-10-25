"""The ``autobrowser`` distribution: wheel contents, the console script, bundled resources."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from src.harness.skills import SKILL_FILE, builtin_skills_root
from src.cli.entry import main

ROOT = Path(__file__).resolve().parents[1]
DOCS_RULES = ROOT / "docs" / "development" / "browser-agent-rules.md"


def test_the_bundled_browser_rules_match_the_reference_copy_in_docs() -> None:
    bundled = builtin_skills_root() / "browser-agent-rules" / SKILL_FILE

    assert bundled.is_relative_to(ROOT / "src")
    assert bundled.read_bytes() == DOCS_RULES.read_bytes(), (
        "src/agent_loop/resources/skills/browser-agent-rules/SKILL.md and "
        "docs/development/browser-agent-rules.md differ; edit both"
    )


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("autobrowser ")


def test_main_py_uses_the_same_entry_point() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "from src.cli.entry import main" in source


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_the_wheel_ships_the_package_the_command_and_the_resources(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    (wheel,) = tmp_path.glob("autobrowser-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        (entry_points,) = [name for name in names if name.endswith("entry_points.txt")]
        scripts = archive.read(entry_points).decode("utf-8")

    top_level = {name.split("/")[0] for name in names}
    assert top_level == {"src", wheel.name.split("-py3")[0] + ".dist-info"}
    assert "src/cli/entry.py" in names
    assert "src/agent_loop/resources/skills/browser-agent-rules/SKILL.md" in names
    assert not [name for name in names if ".pytest_cache" in name or "__pycache__" in name]
    assert "autobrowser = src.cli.entry:main" in scripts
