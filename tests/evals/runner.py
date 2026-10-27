from __future__ import annotations

import os
from pathlib import Path

from src import config
from src.agent_loop.evals import EvalScenario, load_scenario
from src.browser.memory import BrowserMemoryScope
from src.config import MemorySettings
from src.harness.memory_store import MemoryContext, MemoryStore

SCENARIO_DIR = Path(__file__).parent / "scenarios"
MEMORY_SEED_DIR = Path(__file__).parent / "memory_seed"
#: The committed, empty settings profile of hermetic runs: every setting is the code default.
HERMETIC_PROFILE = Path(__file__).resolve().parents[1] / "pipeline" / "hermetic.yaml"
#: Environment settings a hermetic run keeps: the comparison tolerances of the pipeline.
KEPT_ENV_PREFIX = f"{config.ENV_PREFIX}PIPELINE{config.ENV_NESTED_DELIMITER}"


def load_scenarios() -> list[EvalScenario]:
    return [load_scenario(path) for path in sorted(SCENARIO_DIR.glob("*.yaml"))]


def seed_memory(root: Path = MEMORY_SEED_DIR) -> MemoryContext:
    """Read-only ``Memory`` block over a seed directory, from explicit settings only."""

    settings = MemorySettings(persistent_enabled=True)
    return MemoryContext(MemoryStore(root, settings), BrowserMemoryScope())


def isolate_settings(home: Path) -> None:
    """Make every later ``get_settings()`` of this process the code defaults.

    The engine reads some settings itself (loop guards, working notes, observation limits),
    so a personal profile would change eval metrics on one machine only. This drops every
    ``AUTOBROWSER_*`` variable except the ``pipeline`` tolerances, points the home at the
    empty ``home`` directory and the settings file at :data:`HERMETIC_PROFILE`, and hides
    the checkout's legacy ``.env`` — what ``tests/conftest.py`` does for every test. Call it
    once at process start, from a script entry point (it changes the process environment).
    """

    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith(config.ENV_PREFIX) and not upper.startswith(KEPT_ENV_PREFIX):
            del os.environ[name]
    os.environ[config.HOME_ENV_VAR] = str(home)
    os.environ[config.CONFIG_FILE_ENV_VAR] = str(HERMETIC_PROFILE)
    config.LEGACY_ENV_FILE = home / "no-legacy.env"
    config.get_settings.cache_clear()
