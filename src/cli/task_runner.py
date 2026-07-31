"""Task execution adapter for CLI-visible agent runs."""

from __future__ import annotations

import argparse
from typing import Any

from src.cli.output import print_final_state, print_step
from src.harness.runtime import BrowserHarness
from src.harness.session import SessionConfig


async def run_task(
    harness: BrowserHarness,
    task: str,
    args: argparse.Namespace | SessionConfig,
    config: dict[str, Any],
) -> Any:
    """Run one task on an already built harness."""

    as_json = getattr(args, "json", getattr(args, "as_json", False))
    final_update: Any = None
    if args.show_state:
        async for chunk in harness.stream_updates(
            task,
            config=config,
        ):
            final_update = chunk
            for node_name, update in chunk.items():
                print_step(
                    node_name,
                    update,
                    as_json,
                    hide_snapshot=args.hide_snapshot,
                )

        if final_update is None:
            print("Agent finished without state updates.")
        return final_update

    async for chunk in harness.stream_updates(task, config=config):
        final_update = chunk

    result = await _final_state_from_stream(harness, config, final_update)
    print_final_state(result, as_json)
    return result


async def _final_state_from_stream(
    harness: BrowserHarness,
    config: dict[str, Any],
    final_update: Any,
) -> Any:
    get_state_values = getattr(harness, "get_state_values", None)
    if callable(get_state_values):
        state = await get_state_values(config=config)
        if isinstance(state, dict):
            return state
    return _state_from_update(final_update)


def _state_from_update(update: Any) -> Any:
    if not isinstance(update, dict):
        return update
    if len(update) == 1:
        nested = next(iter(update.values()))
        if isinstance(nested, dict):
            return nested
    return update


__all__ = ["run_task"]
