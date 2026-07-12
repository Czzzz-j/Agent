from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrievalCandidate:
    chunk_id: str
    content: str
    metadata: dict[str, Any]
    vector_score: float | None = None
    bm25_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None
    coverage: dict[str, bool] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    query: str
    normalized_query: dict[str, Any]
    candidates: list[RetrievalCandidate]
    selected: list[RetrievalCandidate]
    evidence_sufficient: bool
    refusal_reason: str | None = None
    debug_scores: dict[str, Any] = field(default_factory=dict)
