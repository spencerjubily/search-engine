import asyncio
import hashlib
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from typing import Any

from dotenv import dotenv_values
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import Settings
from app.models import SearchResult

PAPER_SOURCES = "openalex,crossref,arxiv,pubmed,europepmc"
INHERITED_ENV_KEYS = {
    "APPDATA", "HOME", "LOCALAPPDATA", "PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
}


def _server_environment(settings: Settings) -> dict[str, str]:
    child_env = {
        key: value for key, value in os.environ.items() if key in INHERITED_ENV_KEYS
    }
    env_file = os.environ.get("PAPER_SEARCH_MCP_ENV_FILE") or settings.model_config["env_file"]
    configured = dotenv_values(env_file)
    child_env.update(
        {key: value for key, value in configured.items() if key.startswith("PAPER_SEARCH_MCP_") and value}
    )
    child_env.update(
        {key: value for key, value in os.environ.items() if key.startswith("PAPER_SEARCH_MCP_")}
    )
    return child_env


def _paper_result(paper: dict[str, Any]) -> SearchResult | None:
    title = re.sub(r"\s+", " ", str(paper.get("title") or "")).strip()
    url = str(paper.get("url") or paper.get("paper_url") or paper.get("pdf_url") or "").strip()
    doi = str(paper.get("doi") or "").strip()
    if not url and doi:
        url = f"https://doi.org/{doi.removeprefix('https://doi.org/')}"
    if not title or not url.startswith(("https://", "http://")):
        return None
    canonical = url.rstrip("/").split("?", maxsplit=1)[0].lower()
    source_id = "src_" + hashlib.sha256(canonical.encode()).hexdigest()[:12]
    abstract = paper.get("abstract") or paper.get("summary") or paper.get("snippet") or ""
    if isinstance(abstract, list):
        abstract = " ".join(str(part) for part in abstract)
    abstract = re.sub(r"\s+", " ", str(abstract)).strip()[:3000]
    source = str(paper.get("source") or "academic-index")
    year = paper.get("year") or paper.get("published") or paper.get("publication_date")
    return SearchResult(
        id=source_id,
        title=title[:500],
        url=url,
        snippet=abstract,
        provider=f"paper-search-mcp:{source}",
        published_date=str(year)[:80] if year else None,
    )


def _tool_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError("paper-search-mcp returned a tool error")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []):
        text = getattr(block, "text", "")
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    raise ValueError("paper-search-mcp returned no structured paper records")


class PaperSearchMCP:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.paper_mcp_enabled
        self.timeout = settings.request_timeout_seconds
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def start(self) -> None:
        if not self.enabled:
            return
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "paper_search_mcp.server"],
                        env=_server_environment(self.settings),
                    )
                )
            )
            self._session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await asyncio.wait_for(self._session.initialize(), timeout=self.timeout)
            self._stack = stack
        except BaseException:
            await stack.aclose()
            raise

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        if self._session is None:
            raise RuntimeError("paper-search-mcp is not connected")
        response = await asyncio.wait_for(
            self._session.call_tool(
                "search_papers",
                {
                    "query": query,
                    "max_results_per_source": min(max_results, 5),
                    "sources": PAPER_SOURCES,
                },
            ),
            timeout=self.timeout,
        )
        payload = _tool_payload(response)
        papers = payload.get("papers", [])
        if not isinstance(papers, list):
            raise TypeError("paper-search-mcp returned an invalid papers field")
        results = []
        seen: set[str] = set()
        for paper in papers:
            if not isinstance(paper, dict):
                continue
            parsed = _paper_result(paper)
            if parsed and parsed.id not in seen:
                seen.add(parsed.id)
                results.append(parsed)
        return results[:max_results]
