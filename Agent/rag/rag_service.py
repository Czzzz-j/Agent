import math
import re
import time

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from db.knowledge_repository import KnowledgeRepository
from model.factory import chat_model
from rag.retrieval_types import RetrievalCandidate, RetrievalResult
from rag.vector_store import KnowledgeVectorStore
from utils.config_handler import chroma_conf, rag_conf
from utils.logger_handler import logger
from utils.prompt_loader import load_rag_prompts

try:
    from rag.reranker import BGEReranker
except Exception:
    BGEReranker = None


STRICT_REFUSAL = "抱歉，当前知识库中没有足够证据回答这个问题，请您补充更具体的产品信息，或联系人工客服。"

INTENT_TERMS = {
    "troubleshooting": ["故障", "报错", "错误码", "卡住", "不工作", "异响", "漏水"],
    "maintenance": ["清洁", "清洗", "保养", "维护", "除味", "除螨", "擦拭"],
    "repair": ["修复", "维修", "裂缝", "开裂", "烫痕", "白印", "划痕", "松动", "变形"],
    "buying_guide": ["选购", "推荐", "怎么选", "预算", "买哪种"],
}

QUERY_STOP_WORDS = {
    "怎么",
    "怎么办",
    "如何",
    "可以",
    "一下",
    "请问",
    "什么",
    "这个",
    "那个",
    "有点",
}


class RagSummarizeService:
    def __init__(self, repository: KnowledgeRepository | None = None, vector_store: KnowledgeVectorStore | None = None):
        self.repository = repository or KnowledgeRepository()
        self.vector_store = vector_store
        if self.vector_store is None:
            try:
                self.vector_store = KnowledgeVectorStore(
                    collection_name=chroma_conf["collection_name"],
                    embedding_version=rag_conf["embedding_model_name"],
                )
            except Exception as exc:
                logger.warning("[rag] vector store unavailable at startup: %s", exc)
        self.vector_top_k = int(rag_conf.get("vector_top_k", 20))
        self.bm25_top_k = int(rag_conf.get("bm25_top_k", 20))
        self.rerank_top_n = int(rag_conf.get("rerank_top_n", 12))
        self.final_top_n = int(rag_conf.get("final_top_n", 4))
        self.evidence_threshold = float(rag_conf.get("evidence_score_threshold", 0.55))
        self.prompt_template = PromptTemplate.from_template(load_rag_prompts())
        self.chain = self.prompt_template | chat_model | StrOutputParser()
        self.reranker = self._init_reranker()

    def _init_reranker(self):
        if BGEReranker is None:
            logger.warning("[rag] reranker unavailable, fallback to fusion ranking")
            return None
        try:
            return BGEReranker()
        except Exception as exc:
            logger.warning(f"[rag] failed to initialize reranker: {exc}")
            return None

    def retrieve(self, query: str, allowed_domains: list[str] | None = None) -> RetrievalResult:
        started_at = time.time()
        normalized = self._normalize_query(query)
        domain_filter = self._build_domain_filter(allowed_domains)

        vector_error = None
        try:
            vector_hits = self.vector_store.query(
                query,
                self.vector_top_k,
                where=domain_filter,
            )
        except Exception as exc:
            vector_hits = []
            vector_error = str(exc)
            logger.warning("[rag] vector retrieval unavailable, using BM25 only: %s", exc)
        bm25_hits = self._bm25_search(query, allowed_domains=allowed_domains)
        fused_candidates = self._fuse_candidates(normalized, vector_hits, bm25_hits)
        reranked = self._rerank(query, fused_candidates)
        selected = reranked[: self.final_top_n]

        evidence_sufficient, refusal_reason = self._judge_evidence(normalized, selected)
        if vector_hits and bm25_hits:
            retrieval_mode = "hybrid"
        elif vector_hits:
            retrieval_mode = "vector_only"
        else:
            retrieval_mode = "bm25_only"
        top_coverage = selected[0].coverage if selected else {}
        result = RetrievalResult(
            query=query,
            normalized_query=normalized,
            candidates=fused_candidates,
            selected=selected,
            evidence_sufficient=evidence_sufficient,
            refusal_reason=refusal_reason,
            debug_scores={
                "vector_hits": len(vector_hits),
                "bm25_hits": len(bm25_hits),
                "retrieval_mode": retrieval_mode,
                "reranker_available": self.reranker is not None,
                "vector_error": vector_error,
                "top_object_covered": bool(top_coverage.get("object")),
                "top_intent_covered": bool(top_coverage.get("intent")),
                "top_required_terms_covered": bool(
                    top_coverage.get("required_terms", not normalized["required_terms"])
                ),
                "top_lexical_hits": int(top_coverage.get("lexical_hits", 0) or 0),
                "allowed_domains": allowed_domains or [],
                "latency_ms": round((time.time() - started_at) * 1000, 2),
            },
        )
        self._log_result(result)
        return result

    def answer(self, query: str, retrieval_result: RetrievalResult) -> str:
        if not retrieval_result.evidence_sufficient or not retrieval_result.selected:
            return STRICT_REFUSAL

        context_blocks = []
        for index, candidate in enumerate(retrieval_result.selected, start=1):
            meta = candidate.metadata
            header = (
                f"Evidence {index} | source={meta.get('source_name')} | "
                f"category={meta.get('category')} | intent={meta.get('intent')} | page={meta.get('page', 0)}"
            )
            context_blocks.append(f"{header}\n{candidate.content}")

        return self.chain.invoke(
            {
                "input": query,
                "context": "\n\n".join(context_blocks),
                "refusal": STRICT_REFUSAL,
            }
        )

    def rag_summarize(self, query: str, allowed_domains: list[str] | None = None) -> str:
        retrieval_result = self.retrieve(query, allowed_domains=allowed_domains)
        return self.answer(query, retrieval_result)

    def _bm25_search(self, query: str, allowed_domains: list[str] | None = None) -> list[dict]:
        chunks = self.repository.get_active_chunks(allowed_domains=allowed_domains)
        if not chunks:
            return []

        try:
            import jieba
            from rank_bm25 import BM25Okapi
        except Exception:
            logger.warning("[rag] BM25 unavailable, skipping lexical retrieval")
            return []

        corpus = [chunk["keywords"] or list(jieba.cut_for_search(chunk["content"])) for chunk in chunks]
        tokenized_query = [token for token in jieba.cut_for_search(query) if token.strip()]
        if not tokenized_query:
            return []

        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(tokenized_query)
        ranked = [
            item
            for item in sorted(zip(chunks, scores), key=lambda item: item[1], reverse=True)
            if float(item[1]) > 0
        ][: self.bm25_top_k]
        results = []
        for chunk, score in ranked:
            metadata = dict(chunk["metadata"])
            metadata["source_name"] = chunk["source_name"]
            metadata["domain"] = metadata.get("domain") or chunk["domain"]
            metadata["category"] = metadata.get("category") or chunk["category"]
            metadata["embedding_version"] = metadata.get("embedding_version") or chunk["embedding_version"]
            results.append(
                {
                    "chunk_id": chunk["chunk_id"],
                    "content": chunk["content"],
                    "metadata": metadata,
                    "score": float(score),
                }
            )
        return results

    @staticmethod
    def _build_domain_filter(allowed_domains: list[str] | None) -> dict | None:
        if not allowed_domains:
            return None
        domains = [domain for domain in allowed_domains if domain]
        if not domains:
            return None
        if len(domains) == 1:
            return {"domain": domains[0]}
        return {"domain": {"$in": domains}}

    def _fuse_candidates(self, normalized: dict, vector_hits: list[dict], bm25_hits: list[dict]) -> list[RetrievalCandidate]:
        merged: dict[str, RetrievalCandidate] = {}
        for rank, hit in enumerate(vector_hits, start=1):
            candidate = merged.setdefault(
                hit["chunk_id"],
                RetrievalCandidate(chunk_id=hit["chunk_id"], content=hit["content"], metadata=hit["metadata"]),
            )
            candidate.vector_score = 1 / (1 + hit["distance"])
            candidate.fused_score = (candidate.fused_score or 0) + self._rrf(rank)

        for rank, hit in enumerate(bm25_hits, start=1):
            candidate = merged.setdefault(
                hit["chunk_id"],
                RetrievalCandidate(chunk_id=hit["chunk_id"], content=hit["content"], metadata=hit["metadata"]),
            )
            candidate.bm25_score = float(hit["score"])
            candidate.fused_score = (candidate.fused_score or 0) + self._rrf(rank)

        for candidate in merged.values():
            candidate.coverage = self._coverage(normalized, candidate)
            if candidate.coverage.get("object"):
                candidate.fused_score = (candidate.fused_score or 0) + 0.12
            if candidate.coverage.get("intent"):
                candidate.fused_score = (candidate.fused_score or 0) + 0.08
            if candidate.coverage.get("category"):
                candidate.fused_score = (candidate.fused_score or 0) + 0.05

        return sorted(merged.values(), key=lambda item: item.fused_score or 0, reverse=True)

    def _rerank(self, query: str, candidates: list[RetrievalCandidate]) -> list[RetrievalCandidate]:
        candidates = candidates[: self.rerank_top_n]
        if not candidates:
            return []

        if not self.reranker:
            for candidate in candidates:
                candidate.rerank_score = candidate.fused_score or 0
            return candidates

        try:
            raw_scores = self.reranker.score_pairs(query, [candidate.content for candidate in candidates])
            for candidate, raw_score in zip(candidates, raw_scores):
                candidate.rerank_score = self._normalize_rerank_score(float(raw_score))
            return sorted(candidates, key=lambda item: item.rerank_score or 0, reverse=True)
        except Exception as exc:
            logger.warning(f"[rag] rerank failed, fallback to fusion score: {exc}")
            for candidate in candidates:
                candidate.rerank_score = candidate.fused_score or 0
            return candidates

    def _judge_evidence(self, normalized: dict, candidates: list[RetrievalCandidate]) -> tuple[bool, str | None]:
        if not candidates:
            return False, "no_candidates"

        joined_text = "\n".join(candidate.content for candidate in candidates)
        direct_match = any(candidate.coverage.get("object") and candidate.coverage.get("intent") for candidate in candidates)
        if not direct_match:
            return False, "object_or_intent_not_covered"

        required_terms = normalized["required_terms"]
        if required_terms:
            missing_terms = [term for term in required_terms if term.lower() not in joined_text.lower()]
            if missing_terms:
                return False, f"missing_required_terms:{','.join(missing_terms)}"

        top_candidate = candidates[0]
        if self.reranker:
            top_score = top_candidate.rerank_score or 0
            if top_score < self.evidence_threshold:
                return False, f"top_score_below_threshold:{top_score:.3f}"
        else:
            fallback_ok, fallback_reason = self._judge_lexical_fallback(
                normalized,
                top_candidate,
            )
            if not fallback_ok:
                return False, fallback_reason

        for candidate in candidates:
            version_id = candidate.metadata.get("version_id")
            if version_id and not self.repository.is_version_active(version_id):
                return False, f"inactive_version:{version_id}"

        return True, None

    def _normalize_query(self, query: str) -> dict:
        objects = [
            "沙发", "床", "餐桌", "衣柜", "灯具", "地毯", "书柜", "鞋柜",
            "电视柜", "梳妆台", "茶几", "电脑桌", "浴室柜", "橱柜", "扫地机器人", "扫拖机器人",
            "椅子", "办公椅", "吧台椅", "书桌", "书架", "床头柜", "餐边柜",
            "玄关柜", "储物柜", "斗柜", "置物架", "阳台柜", "儿童床",
            "滚刷", "边刷", "滤网", "尘盒", "水箱", "拖布",
        ]
        materials = [
            "布艺",
            "皮质",
            "实木",
            "木质",
            "金属",
            "玻璃",
            "岩板",
            "藤编",
            "板式",
        ]

        detected_objects = [item for item in objects if item in query]
        detected_materials = [item for item in materials if item in query]
        detected_intent = "general"
        for intent, keywords in INTENT_TERMS.items():
            if any(keyword in query for keyword in keywords):
                detected_intent = intent
                break

        numbers = re.findall(r"\d+(?:\.\d+)?", query)
        model_codes = re.findall(r"[A-Za-z]{1,4}\d{2,}[A-Za-z0-9-]*", query)
        error_codes = re.findall(r"(?:E|ERR|C)[-_]?\d{1,4}", query, flags=re.IGNORECASE)
        required_terms = detected_materials + numbers + model_codes + error_codes
        query_terms = self._tokenize_query(query)

        return {
            "query": query,
            "objects": detected_objects,
            "materials": detected_materials,
            "intent": detected_intent,
            "numbers": numbers,
            "model_codes": model_codes,
            "error_codes": error_codes,
            "required_terms": required_terms,
            "query_terms": query_terms,
        }

    def _coverage(self, normalized: dict, candidate: RetrievalCandidate) -> dict[str, bool]:
        haystack = f"{candidate.content}\n{candidate.metadata}".lower()
        object_match = not normalized["objects"] or any(item.lower() in haystack for item in normalized["objects"])
        intent_terms = INTENT_TERMS.get(normalized["intent"], [])
        intent_match = (
            normalized["intent"] == "general"
            or normalized["intent"] in haystack
            or any(term.lower() in haystack for term in intent_terms)
        )
        category = str(candidate.metadata.get("category", "")).lower()
        category_match = not normalized["objects"] or any(item.lower() in category for item in normalized["objects"])
        lexical_hits = sum(
            1 for term in normalized.get("query_terms", []) if term.lower() in haystack
        )
        required_terms_match = all(
            term.lower() in haystack for term in normalized.get("required_terms", [])
        )
        return {
            "object": object_match,
            "intent": intent_match,
            "category": category_match,
            "required_terms": required_terms_match,
            "lexical_hits": lexical_hits,
        }

    def _judge_lexical_fallback(
        self,
        normalized: dict,
        candidate: RetrievalCandidate,
    ) -> tuple[bool, str | None]:
        if candidate.bm25_score is None or candidate.bm25_score <= 0:
            return False, "bm25_direct_match_missing"
        if not candidate.coverage.get("object"):
            return False, "top_candidate_object_mismatch"
        if not candidate.coverage.get("intent"):
            return False, "top_candidate_intent_mismatch"
        if normalized["objects"] and not (
            candidate.coverage.get("category")
            or any(item in candidate.content for item in normalized["objects"])
        ):
            return False, "top_candidate_category_conflict"

        query_terms = normalized.get("query_terms", [])
        lexical_hits = int(candidate.coverage.get("lexical_hits", 0) or 0)
        minimum_hits = 2 if len(query_terms) >= 2 else 1
        if lexical_hits < minimum_hits:
            return False, f"insufficient_lexical_overlap:{lexical_hits}"
        return True, None

    @staticmethod
    def _tokenize_query(query: str) -> list[str]:
        try:
            import jieba

            raw_terms = jieba.cut_for_search(query)
        except Exception:
            raw_terms = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}", query)

        terms: list[str] = []
        for raw_term in raw_terms:
            term = str(raw_term).strip().lower()
            if len(term) <= 1 or term in QUERY_STOP_WORDS or term in terms:
                continue
            terms.append(term)
        return terms[:20]

    def _log_result(self, result: RetrievalResult):
        logger.info(
            "[rag.retrieve] normalized=%s mode=%s evidence=%s refusal=%s latency_ms=%s",
            result.normalized_query,
            result.debug_scores.get("retrieval_mode"),
            result.evidence_sufficient,
            result.refusal_reason,
            result.debug_scores.get("latency_ms"),
        )
        for candidate in result.selected:
            logger.info(
                "[rag.candidate] chunk=%s source=%s version=%s vector=%s bm25=%s fused=%s rerank=%s",
                candidate.chunk_id,
                candidate.metadata.get("source_name"),
                candidate.metadata.get("version_id"),
                candidate.vector_score,
                candidate.bm25_score,
                candidate.fused_score,
                candidate.rerank_score,
            )

    @staticmethod
    def _rrf(rank: int, constant: int = 60) -> float:
        return 1.0 / (constant + rank)

    @staticmethod
    def _normalize_rerank_score(raw_score: float) -> float:
        if 0.0 <= raw_score <= 1.0:
            return raw_score
        return 1.0 / (1.0 + math.exp(-raw_score))
