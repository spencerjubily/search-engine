import pytest

from app.config import Settings
from app.papers import PaperSearchMCP, _paper_result, _server_environment, _tool_payload


def test_paper_record_maps_doi_abstract_and_source():
    paper = _paper_result({
        "title": "A useful paper",
        "doi": "10.1000/example",
        "abstract": "  Abstract   text ",
        "source": "crossref",
        "year": 2025,
    })
    assert paper is not None
    assert str(paper.url) == "https://doi.org/10.1000/example"
    assert paper.snippet == "Abstract text"
    assert paper.provider == "paper-search-mcp:crossref"
    assert paper.published_date == "2025"


def test_paper_record_ignores_unlinkable_incomplete_rows():
    assert _paper_result({"title": "No URL or DOI"}) is None


def test_mcp_tool_text_result_decodes_json_payload():
    class TextBlock:
        text = '{"papers": []}'

    class ToolResult:
        def __init__(self):
            self.isError = False
            self.structuredContent = None
            self.content = [TextBlock()]

    assert _tool_payload(ToolResult()) == {"papers": []}


def test_mcp_subprocess_receives_only_paper_credentials(monkeypatch):
    monkeypatch.setenv("PAPER_SEARCH_MCP_CORE_API_KEY", "core-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "must-not-leak")
    child_env = _server_environment(Settings())
    assert child_env["PAPER_SEARCH_MCP_CORE_API_KEY"] == "core-secret"
    assert "TAVILY_API_KEY" not in child_env


@pytest.mark.asyncio
async def test_stdio_server_handshake_exposes_search_papers():
    server = PaperSearchMCP(Settings(request_timeout_seconds=30))
    await server.start()
    try:
        tools = await server._session.list_tools()
        assert any(tool.name == "search_papers" for tool in tools.tools)
    finally:
        await server.close()
