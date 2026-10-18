"""Read-only local prompt resource loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptResource:
    """A deterministic local prompt resource rendered as one system context block.

    Which resources a session uses is decided by its composition root (the browser rules
    when a browser MCP server is configured), never by matching tool names or task words.
    """

    name: str
    #: Heading of the context block the resource is rendered under.
    title: str
    path: Path
    description: str

    def load(self) -> str:
        """Read the resource as UTF-8 text."""

        return self.path.read_text(encoding="utf-8")


def browser_agent_rules_resource() -> PromptResource:
    """Return the built-in browser rules resource."""

    return PromptResource(
        name="browser-agent-rules",
        title="Browser Rules",
        path=(
            Path(__file__).resolve().parents[2]
            / "docs"
            / "development"
            / "browser-agent-rules.md"
        ),
        description="Snapshot-driven browser interaction and extraction rules.",
    )


__all__ = ["PromptResource", "browser_agent_rules_resource"]
