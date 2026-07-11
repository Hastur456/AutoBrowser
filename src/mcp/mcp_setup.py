"""MCP setup with MultiServerMCPClient."""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv()


async def setup_mcp() -> list[Any]:
    """Connect to the Playwright MCP server and return LangChain tools."""

    port = os.getenv("PORT", "9222")
    mcp_servers = {
        "browser": {
            "transport": "stdio",
            "command": "npx",
            "args": [
                "-y",
                "@playwright/mcp@latest",
                "--cdp-endpoint",
                f"http://localhost:{port}",
            ],
        }
    }

    client = MultiServerMCPClient(mcp_servers)
    return await client.get_tools()
