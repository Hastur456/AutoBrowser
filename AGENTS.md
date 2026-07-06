# Repository Guidelines

## Project Structure & Module Organization

This is a Python repository for an AutoBrowser/LangGraph agent. Core code lives in `src/`. The main agent modules are in `src/agent/`, MCP integration is in `src/mcp/`, and reusable graph pieces are under `src/subgraphs/` with `planner` and `executor` packages. Tests should live in `tests/`; the directory currently exists but has no committed test files. Runtime or local-only folders such as `.venv/`, `.pytest_cache/`, `node_modules/`, `.codegraph/`, and `profile/` should not be treated as source.

## Build, Test, and Development Commands

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Run the test suite with:

```powershell
python -m pytest
```

Run targeted tests with `python -m pytest tests\path\to_test.py`. If adding a local server entry point, prefer `python -m uvicorn module:app --reload` and document the module path.

## Coding Style & Naming Conventions

Use Python 3.12-compatible code. Follow PEP 8 with 4-space indentation, snake_case for functions and modules, PascalCase for classes, and UPPER_SNAKE_CASE for constants. Keep graph node, router, state, and prompt code in the existing `nodes.py`, `routers.py`, `state.py`, and `prompts.py` pattern. Add type hints for public functions and graph state structures.

## Testing Guidelines

Use `pytest` and `pytest-asyncio` for asynchronous graph or MCP behavior. Name test files `test_*.py` and test functions `test_*`. Prefer focused unit tests for routers and state transitions, plus integration tests for graph assembly and tool execution boundaries. Do not require external services in default tests unless they are skipped or mocked.

## Commit & Pull Request Guidelines

Recent history uses short messages and Conventional Commit-style prefixes such as `feat(agent): ...` and `fix(execute): ...`. Use imperative, scoped commit subjects when possible. Pull requests should describe the behavioral change, list tests run, mention any MCP/Ollama assumptions, and include screenshots or logs only when UI or browser-observation behavior changes.

# Playwright MCP Development Rules

This project is NOT a traditional Playwright project.

The browser agent must follow Playwright MCP semantics.

Source of truth:
- browser_snapshot

Element identity:
- ref=e123

Preferred interaction:
- browser_click(ref)
- browser_type(ref)
- browser_hover(ref)

Do NOT:
- guess CSS selectors
- generate XPath
- rely on class names
- assume DOM structure

If the snapshot does not expose the needed element:
1. Capture another snapshot.
2. Increase snapshot depth if appropriate.
3. Use browser_evaluate only if the snapshot cannot answer the question.

The agent is snapshot-driven, not selector-driven.