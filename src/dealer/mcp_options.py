import os

from langchain_mcp_adapters.client import MultiServerMCPClient


async def get_options_tools():
    """Returns LangChain-bindable tools for the official Alpaca MCP server, restricted to
    read-only toolsets -- order placement stays exclusively in Floor Broker's alpaca-py
    execution path, never via MCP tool-calling (least-privilege split, see plan Global
    Constraints)."""
    client = MultiServerMCPClient(
        {
            "alpaca": {
                "transport": "stdio",
                "command": "alpaca-mcp-server",
                "args": [],
                "env": {
                    "ALPACA_API_KEY": os.environ["ALPACA_PAPER_API_KEY2"],
                    "ALPACA_SECRET_KEY": os.environ["ALPACA_PAPER_API_SECRET2"],
                    "ALPACA_PAPER_TRADE": "True",
                    "ALPACA_TOOLSETS": "assets,options-data,account",
                },
            }
        }
    )
    return await client.get_tools()
