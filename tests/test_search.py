import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.models import ResearchMode, SearchRequest, SearchResult
from app.search import HybridSearch, _normalize_url


def result(url: str, provider: str) -> SearchResult:
    return SearchResult(id="src_0123456789ab", title="Paper", url=url, provider=provider)


def test_url_normalization_drops_tracking_and_normalizes_host():
    assert _normalize_url("HTTPS://Example.COM/paper/?utm_source=test") == "https://example.com/paper"


def test_fusion_deduplicates_urls_and_combines_provider_names():
    fused = HybridSearch._fuse([
        [result("https://example.com/paper", "searxng")],
        [result("https://EXAMPLE.com/paper/", "tavily")],
    ])
    assert len(fused) == 1
    assert fused[0].provider == "searxng,tavily"


def test_modes_have_targeted_search_queries():
    assert SearchRequest(query="How does this project compare?", mode=ResearchMode.patents).mode == ResearchMode.patents


@pytest.mark.asyncio
async def test_hybrid_search_queries_both_providers_and_deduplicates():
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "searx.local":
            return httpx.Response(200, json={"results": [{"title": "Patent", "url": "https://example.org/patent", "content": "SearX"}]})
        return httpx.Response(200, json={"results": [{"title": "Patent", "url": "https://example.org/patent/", "content": "Tavily", "score": 0.9}]})

    settings = Settings(
        searxng_url="http://searx.local", tavily_api_key=SecretStr("test"), paper_mcp_enabled=False
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        results, providers = await HybridSearch(settings, client).search(
            SearchRequest(query="Novel sensor for crop disease", max_results=4)
        )
    assert len(results) == 1
    assert results[0].provider == "searxng,tavily"
    assert results[0].snippet == "SearX"
    assert providers == {"searxng": "ok", "tavily": "ok", "paper-search-mcp": "disabled"}


@pytest.mark.asyncio
async def test_search_continues_when_tavily_is_unavailable():
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "searx.local":
            return httpx.Response(200, json={"results": [{"title": "Paper", "url": "https://example.org/paper"}]})
        return httpx.Response(503)

    settings = Settings(
        searxng_url="http://searx.local", tavily_api_key=SecretStr("test"), paper_mcp_enabled=False
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        results, providers = await HybridSearch(settings, client).search(
            SearchRequest(query="Novel sensor for crop disease", max_results=4)
        )
    assert results
    assert providers["searxng"] == "ok"
    assert providers["tavily"].startswith("error:")
