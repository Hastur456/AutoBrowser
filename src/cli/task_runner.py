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
    if args.show_state:
        final_update: Any = None
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

    result = await harness.run(task, config=config)
    print_final_state(result, as_json)
    return result


__all__ = ["run_task"]
