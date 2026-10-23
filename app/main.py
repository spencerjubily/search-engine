import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from app.analysis import OllamaAnalyzer
from app.config import Settings, get_settings
from app.models import HealthResponse, ResearchResponse, SearchRequest, SearchResponse
from app.papers import PaperSearchMCP
from app.search import HybridSearch, SearchUnavailable

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout_seconds))
    app.state.request_slots = asyncio.Semaphore(settings.max_concurrent_requests)
    app.state.paper_mcp = PaperSearchMCP(settings)
    try:
        await app.state.paper_mcp.start()
    except Exception:
        logger.exception("paper-search-mcp failed to initialize; continuing without paper MCP")
    app.state.search = HybridSearch(settings, app.state.http, app.state.paper_mcp)
    app.state.analyzer = OllamaAnalyzer(settings, app.state.http)
    yield
    await app.state.paper_mcp.close()
    await app.state.http.aclose()


app = FastAPI(title="Research Search Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


async def authorize(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected = request.app.state.settings.agent_api_key
    if expected and (authorization or "") != f"Bearer {expected.get_secret_value()}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


async def _run_search(request: Request, payload: SearchRequest):
    try:
        return await request.app.state.search.search(payload)
    except SearchUnavailable as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "providers": exc.providers}) from exc


@app.get("/health", response_model=HealthResponse)
async def health(request: Request):
    settings: Settings = request.app.state.settings
    checks = await asyncio.gather(
        request.app.state.http.get(str(settings.searxng_url).rstrip("/") + "/search", params={"q": "health", "format": "json"}),
        request.app.state.http.get(str(settings.ollama_url).rstrip("/") + "/api/tags"),
        return_exceptions=True,
    )
    providers = {
        "searxng": "ok" if isinstance(checks[0], httpx.Response) and checks[0].is_success else "unavailable",
        "ollama": "ok" if isinstance(checks[1], httpx.Response) and checks[1].is_success else "unavailable",
        "tavily": "configured" if settings.tavily_api_key else "not_configured",
        "paper_search_mcp": (
            "disabled" if not settings.paper_mcp_enabled
            else "ok" if request.app.state.paper_mcp.connected
            else "unavailable"
        ),
    }
    status = "ok" if providers["searxng"] == "ok" and providers["ollama"] == "ok" else "degraded"
    return HealthResponse(status=status, providers=providers, timestamp=datetime.now(UTC))


@app.post("/v1/search", response_model=SearchResponse, dependencies=[Depends(authorize)])
async def search(payload: SearchRequest, request: Request):
    started = time.perf_counter()
    async with request.app.state.request_slots:
        results, providers = await _run_search(request, payload)
    return SearchResponse(
        query=payload.query, mode=payload.mode, results=results, providers=providers,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


@app.post("/v1/research", response_model=ResearchResponse, dependencies=[Depends(authorize)])
async def research(payload: SearchRequest, request: Request):
    started = time.perf_counter()
    async with request.app.state.request_slots:
        results, providers = await _run_search(request, payload)
        try:
            analysis = await request.app.state.analyzer.analyze(payload, results)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"Analysis provider failed: {type(exc).__name__}") from exc
    return ResearchResponse(
        query=payload.query, mode=payload.mode, analysis=analysis, sources=results,
        providers=providers, elapsed_ms=int((time.perf_counter() - started) * 1000),
        model=request.app.state.settings.ollama_model,
    )
