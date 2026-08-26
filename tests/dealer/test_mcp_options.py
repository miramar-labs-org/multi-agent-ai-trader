from src.dealer import mcp_options


def test_get_options_tools_config_uses_read_only_toolsets(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, connections):
            captured["connections"] = connections

        async def get_tools(self):
            return ["fake-tool"]

    monkeypatch.setenv("ALPACA_PAPER_API_KEY2", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_API_SECRET2", "test-secret")
    monkeypatch.setattr(mcp_options, "MultiServerMCPClient", FakeClient)

    import asyncio

    tools = asyncio.run(mcp_options.get_options_tools())

    assert tools == ["fake-tool"]
    conn = captured["connections"]["alpaca"]
    assert conn["transport"] == "stdio"
    assert conn["command"] == "alpaca-mcp-server"
    assert conn["env"]["ALPACA_API_KEY"] == "test-key"
    assert conn["env"]["ALPACA_SECRET_KEY"] == "test-secret"
    assert conn["env"]["ALPACA_TOOLSETS"] == "assets,options-data,account"
