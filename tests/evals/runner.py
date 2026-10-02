from __future__ import annotations

from pathlib import Path

from src.agent_loop.evals import EvalScenario, load_scenario
from src.browser.memory import BrowserMemoryScope
from src.config import MemorySettings
from src.harness.memory_store import MemoryContext, MemoryStore

SCENARIO_DIR = Path(__file__).parent / "scenarios"
MEMORY_SEED_DIR = Path(__file__).parent / "memory_seed"


def load_scenarios() -> list[EvalScenario]:
    return [load_scenario(path) for path in sorted(SCENARIO_DIR.glob("*.yaml"))]


def seed_memory(root: Path = MEMORY_SEED_DIR) -> MemoryContext:
    """Read-only ``Memory`` block over a seed directory, from explicit settings only."""

    settings = MemorySettings(persistent_enabled=True)
    return MemoryContext(MemoryStore(root, settings), BrowserMemoryScope())
