import os

from langchain_mcp_adapters.client import MultiServerMCPClient

from src.common.alpaca_client import live_account_env_names


async def get_options_tools():
    """Returns LangChain-bindable tools for the official Alpaca MCP server, restricted to
    read-only toolsets -- order placement stays exclusively in Floor Broker's alpaca-py
    execution path, never via MCP tool-calling (least-privilege split, see plan Global
    Constraints). Resolves the live account's credentials the same config-driven way as
    src.common.alpaca_client's trading_client/option_data_client -- so pointing alpaca.live at a
    different paper account in config.yaml also repoints which credentials this MCP subprocess is
    launched with, not just the direct alpaca-py clients."""
    key_env, secret_env = live_account_env_names()
    client = MultiServerMCPClient(
        {
            "alpaca": {
                "transport": "stdio",
                "command": "alpaca-mcp-server",
                "args": [],
                "env": {
                    "ALPACA_API_KEY": os.environ[key_env],
                    "ALPACA_SECRET_KEY": os.environ[secret_env],
                    "ALPACA_PAPER_TRADE": "True",
                    "ALPACA_TOOLSETS": "assets,options-data,account",
                },
            }
        }
    )
    return await client.get_tools()
