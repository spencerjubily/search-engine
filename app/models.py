from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class ResearchMode(StrEnum):
    novelty = "novelty"
    papers = "papers"
    patents = "patents"


class SearchRequest(BaseModel):
    query: str = Field(min_length=8, max_length=700)
    mode: ResearchMode = ResearchMode.novelty
    max_results: int = Field(default=10, ge=1, le=30)
    language: str = Field(default="en", min_length=2, max_length=8)


class SearchResult(BaseModel):
    id: str
    title: str
    url: HttpUrl
    snippet: str = ""
    provider: str
    score: float | None = None
    published_date: str | None = None


class SearchResponse(BaseModel):
    query: str
    mode: ResearchMode
    results: list[SearchResult]
    providers: dict[str, str]
    elapsed_ms: int


class Claim(BaseModel):
    claim: str
    evidence: str
    source_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class Analysis(BaseModel):
    executive_summary: str
    novelty_assessment: str
    relevant_papers: list[str] = Field(default_factory=list)
    patent_landscape: list[str] = Field(default_factory=list)
    gaps_and_risks: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ResearchResponse(BaseModel):
    query: str
    mode: ResearchMode
    analysis: Analysis
    sources: list[SearchResult]
    providers: dict[str, str]
    elapsed_ms: int
    model: str


class HealthResponse(BaseModel):
    status: str
    providers: dict[str, str]
    timestamp: datetime
