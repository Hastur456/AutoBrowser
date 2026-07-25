"""LangSmith tracing environment helpers."""

from __future__ import annotations

import os

DEFAULT_LANGSMITH_PROJECT = "autobrowser"


def configure_langsmith_tracing() -> bool:
    """Normalize LangSmith tracing environment variables.

    LangChain/LangGraph read tracing settings from environment variables. This
    keeps the supported LangSmith names and legacy LangChain names in sync so a
    project .env can use either convention.
    """

    tracing = os.getenv("LANGSMITH_TRACING") or os.getenv("LANGCHAIN_TRACING_V2")
    enabled = str(tracing).lower() in {"1", "true", "yes", "on"}

    if enabled:
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")

    project = (
        os.getenv("LANGSMITH_PROJECT")
        or os.getenv("LANGCHAIN_PROJECT")
        or DEFAULT_LANGSMITH_PROJECT
    )
    os.environ.setdefault("LANGSMITH_PROJECT", project)
    os.environ.setdefault("LANGCHAIN_PROJECT", project)

    return enabled


__all__ = ["DEFAULT_LANGSMITH_PROJECT", "configure_langsmith_tracing"]
