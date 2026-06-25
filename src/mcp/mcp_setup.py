"""MCP setup with MultyServerAdapter"""


import os
from langchain_mcp_adapters.client import MultiServerMCPClient
from dotenv import load_dotenv

load_dotenv()

async def setup_mcp():
    """
    Подключение к MCP-playwright серверу
    """
    mcp_servers = {
        "browser": {
            "transport": "stdio",
            "command": "npx",
            "args": [
                "-y", 
                "@playwright/mcp@latest", 
                "--cdp-endpoint", 
                f"http://localhost:{os.getenv("PORT")}"
            ],
        }
    }

    client = MultiServerMCPClient(mcp_servers)

    tools = await client.get_tools()

    return tools
