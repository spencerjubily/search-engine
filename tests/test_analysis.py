from app.analysis import validate_citations
from app.models import Analysis, Claim, SearchResult


def test_analysis_discards_unknown_source_ids():
    source = SearchResult(
        id="src_aaaaaaaaaaaa", title="Evidence", url="https://example.org", provider="searxng"
    )
    analysis = Analysis(
        executive_summary="Summary", novelty_assessment="Unclear",
        claims=[Claim(claim="A", evidence="B", source_ids=["src_aaaaaaaaaaaa", "src_bbbbbbbbbbbb"], confidence=0.5)],
    )
    validate_citations(analysis, [source])
    assert analysis.claims[0].source_ids == ["src_aaaaaaaaaaaa"]
