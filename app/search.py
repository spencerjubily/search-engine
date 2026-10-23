import asyncio
import hashlib
import re
import time
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.shared.exceptions import McpError

from app.config import Settings
from app.models import ResearchMode, SearchRequest, SearchResult
from app.papers import PaperSearchMCP

MODE_HINTS = {
    ResearchMode.novelty: [
        "prior art existing solutions technical approach",
        "research papers and patents related work",
    ],
    ResearchMode.papers: [
        "academic research paper survey literature review",
        "site:arxiv.org OR site:pubmed.ncbi.nlm.nih.gov research paper",
    ],
    ResearchMode.patents: [
        "patent prior art patent application claims",
        "site:patents.google.com OR site:patentscope.wipo.int patent",
    ],
}


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower() or "https", host + port, path, "", ""))


def _source_id(url: str) -> str:
    return "src_" + hashlib.sha256(_normalize_url(url).encode()).hexdigest()[:12]


def _clean_text(value: object, limit: int = 3000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


class HybridSearch:
    def __init__(
        self, settings: Settings, client: httpx.AsyncClient, paper_mcp: PaperSearchMCP | None = None
    ):
        self.settings = settings
        self.client = client
        self.paper_mcp = paper_mcp
        self._limit = asyncio.Semaphore(settings.max_concurrent_requests)

    async def search(self, request: SearchRequest) -> tuple[list[SearchResult], dict[str, str]]:
        queries = [request.query, *(f"{request.query} {hint}" for hint in MODE_HINTS[request.mode])]
        tasks = [self._provider_search("searxng", query, request) for query in queries]
        if self.settings.tavily_api_key and self.settings.tavily_api_key.get_secret_value():
            tasks.extend(self._provider_search("tavily", query, request) for query in queries)
        if self.paper_mcp and self.paper_mcp.connected and request.mode in (
            ResearchMode.novelty, ResearchMode.papers
        ):
            tasks.append(self._paper_search(request))
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        rankings: list[list[SearchResult]] = []
        provider_outcomes: dict[str, list[tuple[str, list[SearchResult]]]] = {
            "searxng": [], "tavily": [], "paper-search-mcp": []
        }
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                continue
            provider, status, provider_results = outcome
            provider_outcomes[provider].append((status, provider_results))
            rankings.append(provider_results)
        providers: dict[str, str] = {}
        for provider, attempts in provider_outcomes.items():
            if not attempts:
                if provider == "tavily" or (provider == "paper-search-mcp" and not self.settings.paper_mcp_enabled):
                    providers[provider] = "disabled"
                elif provider == "paper-search-mcp" and not (self.paper_mcp and self.paper_mcp.connected):
                    providers[provider] = "unavailable"
                elif provider == "paper-search-mcp":
                    providers[provider] = "not_used"
                else:
                    providers[provider] = "unavailable"
            elif any(items for _, items in attempts):
                providers[provider] = "ok"
            else:
                errors = [status for status, _ in attempts if status.startswith("error:")]
                providers[provider] = errors[0] if errors else "empty"
        fused = self._fuse(rankings)
        if not fused:
            raise SearchUnavailable("All configured search providers failed or returned no results", providers)
        return fused[: min(request.max_results, self.settings.max_search_results)], providers

    async def _paper_search(
        self, request: SearchRequest
    ) -> tuple[str, str, list[SearchResult]]:
        try:
            results = await self.paper_mcp.search(request.query, request.max_results)
            return "paper-search-mcp", "ok" if results else "empty", results
        except (TimeoutError, RuntimeError, ValueError, TypeError, OSError, McpError) as exc:
            return "paper-search-mcp", f"error: {type(exc).__name__}", []

    async def _provider_search(
        self, provider: str, query: str, request: SearchRequest
    ) -> tuple[str, str, list[SearchResult]]:
        async with self._limit:
            try:
                if provider == "searxng":
                    rows = await self._searxng(query, request)
                else:
                    rows = await self._tavily(query, request)
                return provider, "ok" if rows else "empty", rows
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                return provider, f"error: {type(exc).__name__}", []

    async def _searxng(self, query: str, request: SearchRequest) -> list[SearchResult]:
        response = await self.client.get(
            str(self.settings.searxng_url).rstrip("/") + "/search",
            params={"q": query, "format": "json", "language": request.language, "safesearch": 1},
        )
        response.raise_for_status()
        payload = response.json()
        return [
            SearchResult(
                id=_source_id(row["url"]),
                title=_clean_text(row.get("title"), 500) or row["url"],
                url=row["url"],
                snippet=_clean_text(row.get("content")),
                provider="searxng",
                score=row.get("score") if isinstance(row.get("score"), (int, float)) else None,
                published_date=_clean_text(row.get("publishedDate"), 80) or None,
            )
            for row in payload.get("results", [])
            if isinstance(row, dict) and isinstance(row.get("url"), str) and _normalize_url(row["url"])
        ]

    async def _tavily(self, query: str, request: SearchRequest) -> list[SearchResult]:
        response = await self.client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self.settings.tavily_api_key.get_secret_value()}"},
            json={
                "query": query,
                "search_depth": "advanced" if request.mode != ResearchMode.novelty else "basic",
                "max_results": min(request.max_results, 10),
                "topic": "general",
                "include_answer": False,
                "include_raw_content": request.mode in (ResearchMode.papers, ResearchMode.patents),
            },
        )
        response.raise_for_status()
        payload = response.json()
        return [
            SearchResult(
                id=_source_id(row["url"]),
                title=_clean_text(row.get("title"), 500) or row["url"],
                url=row["url"],
                snippet=_clean_text(row.get("raw_content") or row.get("content")),
                provider="tavily",
                score=row.get("score") if isinstance(row.get("score"), (int, float)) else None,
                published_date=_clean_text(row.get("published_date"), 80) or None,
            )
            for row in payload.get("results", [])
            if isinstance(row, dict) and isinstance(row.get("url"), str) and _normalize_url(row["url"])
        ]

    @staticmethod
    def _fuse(rankings: Iterable[Iterable[SearchResult]]) -> list[SearchResult]:
        by_url: dict[str, SearchResult] = {}
        ranks: dict[str, float] = {}
        for ranking in rankings:
            for rank, item in enumerate(ranking, start=1):
                key = _normalize_url(str(item.url))
                if not key:
                    continue
                ranks[key] = ranks.get(key, 0) + 1 / (60 + rank)
                current = by_url.get(key)
                if current is None:
                    by_url[key] = item
                else:
                    current.snippet = current.snippet or item.snippet
                    if item.score is not None:
                        current.score = max(current.score or 0, item.score)
                    if item.provider not in current.provider.split(","):
                        current.provider += "," + item.provider
        return sorted(by_url.values(), key=lambda item: ranks[_normalize_url(str(item.url))], reverse=True)


class SearchUnavailable(Exception):
    def __init__(self, message: str, providers: dict[str, str]):
        super().__init__(message)
        self.providers = providers


async def timed_search(
    engine: HybridSearch, request: SearchRequest
) -> tuple[list[SearchResult], dict[str, str], int]:
    started = time.perf_counter()
    results, providers = await engine.search(request)
    return results, providers, int((time.perf_counter() - started) * 1000)
