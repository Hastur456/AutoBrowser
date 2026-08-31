"""Neutral model/LLM defaults for the AutoBrowser CLI and scripts.

Historically ``DEFAULT_OLLAMA_MODEL`` (and the ``ChatOllama`` factory) lived in
``src/agent/agent.py`` next to the now-removed LangGraph graph builder. This module
is the graph-free home for those constants so the CLI and batch/evals scripts no
longer import from ``src/agent/``.
"""

DEFAULT_OLLAMA_MODEL = "gpt-oss:20b-cloud"

__all__ = ["DEFAULT_OLLAMA_MODEL"]
