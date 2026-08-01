"""Read-only local prompt resource loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptResource:
    """Metadata for a deterministic local prompt resource."""

    name: str
    path: Path
    description: str
    trigger_terms: tuple[str, ...]
    scopes: tuple[str, ...]

    def load(self) -> str:
        """Read the resource as UTF-8 text."""

        return self.path.read_text(encoding="utf-8")


def browser_agent_rules_resource() -> PromptResource:
    """Return the built-in browser rules resource."""

    return PromptResource(
        name="browser-agent-rules",
        path=(
            Path(__file__).resolve().parents[2]
            / "docs"
            / "development"
            / "browser-agent-rules.md"
        ),
        description="Snapshot-driven browser interaction and extraction rules.",
        trigger_terms=(
            "open",
            "browse",
            "browser",
            "click",
            "extract",
            "inspect a page",
            "search a site",
            "type",
        ),
        scopes=("browser", "search", "page-extraction"),
    )


__all__ = ["PromptResource", "browser_agent_rules_resource"]
