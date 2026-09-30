"""External process used by the command-hook tests: ``python command_hook_fixture.py <mode>``."""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    mode = sys.argv[1]
    event = json.loads(sys.stdin.read())
    if mode == "echo":
        print(json.dumps({
            "additional_context": (
                f"{event['name']}|{event['tool']}|{event['args']}|"
                f"{os.environ['AUTOBROWSER_HOOK_EVENT']}|{os.getcwd()}"
            )
        }))
    elif mode == "block":
        print(f"no navigation to {event['args'].get('url')}", file=sys.stderr)
        return 2
    elif mode == "rewrite":
        print(json.dumps({"decision": "allow", "updated_input": {"url": "https://safe.test"}}))
    elif mode == "text":
        print("remember the budget")
    elif mode == "crash":
        print("boom", file=sys.stderr)
        return 1
    elif mode == "sleep":
        time.sleep(30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
