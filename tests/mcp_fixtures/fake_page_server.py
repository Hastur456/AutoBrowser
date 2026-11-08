"""A tiny stdio MCP "browser" for playback tests: a shop page whose element refs change on
every load, like Playwright MCP's ``[ref=…]`` markers (no network, no browser).

Tools: ``navigate`` (state-changing), ``snapshot`` (``readOnlyHint``), ``click`` (by ref).
Arguments: ``--ref-base N`` shifts the refs of a process (another run of the browser),
``--layout changed`` renames the Specs tab (the recorded element is gone), ``--layout moved``
puts the product on another path.
"""

from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

parser = argparse.ArgumentParser()
parser.add_argument("--ref-base", type=int, default=10)
parser.add_argument("--layout", choices=("normal", "changed", "moved"), default="normal")
options = parser.parse_args()

server = FastMCP("page", log_level="WARNING")
_page = {"url": "about:blank", "loads": 0, "tab": ""}


def _refs() -> dict[str, str]:
    base = options.ref_base + 10 * _page["loads"]
    tab = "Characteristics" if options.layout == "changed" else "Specs"
    return {
        f"e{base}": f'tab "Description"',
        f"e{base + 1}": f'tab "{tab}"',
        f"e{base + 2}": 'button "Buy now"',
    }


def _render() -> str:
    lines = [f"- Page URL: {_page['url']}"]
    if _page["url"] == "about:blank":
        return lines[0]
    lines.append(f"- heading \"Kettle\" [ref=e{options.ref_base + 10 * _page['loads'] + 9}]")
    lines.append("- tablist:")
    for ref, label in _refs().items():
        indent = "  " if label.startswith("tab") else ""
        lines.append(f"{indent}- {label} [ref={ref}]")
    if _page["tab"]:
        lines.append(f"- tabpanel \"{_page['tab']}\":")
        lines.append("  - cell \"Price 42\"")
    return "\n".join(lines)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def navigate(url: str) -> str:
    """Open a page."""
    if options.layout == "moved":
        url = url.replace("/item/", "/product/")
    _page.update(url=url, loads=_page["loads"] + 1, tab="")
    return _render()


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def snapshot() -> str:
    """The accessibility snapshot of the current page."""
    return _render()


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def click(element: str, target: str) -> str:
    """Click the element with the ref ``target``."""
    label = _refs().get(target)
    if label is None:
        raise ValueError(f"Ref {target} not found in the current page snapshot.")
    _page["tab"] = label.split('"')[1]
    return _render()


if __name__ == "__main__":
    server.run()
