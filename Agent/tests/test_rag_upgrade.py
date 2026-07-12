import tempfile
import unittest
from pathlib import Path

from rag.document_parser import parse_text_document
from rag.rag_service import RagSummarizeService
from rag.retrieval_types import RetrievalCandidate
from utils.file_handler import get_file_sha256_hex


class RagUpgradeTest(unittest.TestCase):
    def test_sha256_hash_reads_full_file(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as file_obj:
            file_obj.write(b"a" * 5000 + b"b" * 5000)
            file_path = Path(file_obj.name)

        try:
            digest = get_file_sha256_hex(str(file_path))
            self.assertIsNotNone(digest)
            self.assertEqual(len(digest), 64)
        finally:
            file_path.unlink(missing_ok=True)

    def test_faq_document_is_split_by_question_answer(self):
        chunks = parse_text_document(
            "问题1：布艺沙发怎么清洁？\n先吸尘，再局部测试清洁剂。\n\n问题2：多久保养一次？\n建议按月检查。",
            "沙发FAQ.txt",
        )

        self.assertEqual(len(chunks), 2)
        self.assertIn("布艺沙发怎么清洁", chunks[0]["content"])
        self.assertEqual(chunks[0]["intent"], "maintenance")

    def test_evidence_requires_required_terms(self):
        service = RagSummarizeService.__new__(RagSummarizeService)
        service.evidence_threshold = 0.55
        service.reranker = object()
        service.repository = type("Repo", (), {"is_version_active": staticmethod(lambda version_id: True)})()

        normalized = {
            "objects": ["扫地机器人"],
            "intent": "troubleshooting",
            "required_terms": ["E12"],
        }
        candidates = [
            RetrievalCandidate(
                chunk_id="c1",
                content="扫地机器人报错时请先检查滚刷是否卡住。",
                metadata={"version_id": "v1", "category": "扫地机器人"},
                rerank_score=0.91,
                coverage={"object": True, "intent": True, "category": True},
            )
        ]

        enough, reason = service._judge_evidence(normalized, candidates)
        self.assertFalse(enough)
        self.assertIn("missing_required_terms", reason)

    def test_evidence_passes_when_object_intent_and_terms_match(self):
        service = RagSummarizeService.__new__(RagSummarizeService)
        service.evidence_threshold = 0.55
        service.reranker = object()
        service.repository = type("Repo", (), {"is_version_active": staticmethod(lambda version_id: True)})()

        normalized = {
            "objects": ["扫地机器人"],
            "intent": "troubleshooting",
            "required_terms": ["E12"],
        }
        candidates = [
            RetrievalCandidate(
                chunk_id="c1",
                content="扫地机器人出现 E12 错误码时，请检查滚刷是否卡住。",
                metadata={"version_id": "v1", "category": "扫地机器人"},
                rerank_score=0.91,
                coverage={"object": True, "intent": True, "category": True},
            )
        ]

        enough, reason = service._judge_evidence(normalized, candidates)
        self.assertTrue(enough)
        self.assertIsNone(reason)

    def test_allowed_domains_filter_reaches_vector_and_bm25_layers(self):
        class FakeVectorStore:
            def __init__(self):
                self.where = None

            def query(self, query, top_k, where=None):
                self.where = where
                return []

        class FakeRepository:
            def __init__(self):
                self.allowed_domains = None

            def get_active_chunks(self, allowed_domains=None):
                self.allowed_domains = allowed_domains
                return []

            @staticmethod
            def is_version_active(version_id):
                return True

        service = RagSummarizeService.__new__(RagSummarizeService)
        service.repository = FakeRepository()
        service.vector_store = FakeVectorStore()
        service.vector_top_k = 20
        service.bm25_top_k = 20
        service.rerank_top_n = 12
        service.final_top_n = 4
        service.evidence_threshold = 0.55
        service.reranker = None

        result = service.retrieve("沙发怎么清洁", allowed_domains=["furniture"])

        self.assertEqual(service.vector_store.where, {"domain": "furniture"})
        self.assertEqual(service.repository.allowed_domains, ["furniture"])
        self.assertFalse(result.evidence_sufficient)

    def test_bm25_fallback_does_not_compare_rrf_with_reranker_threshold(self):
        service = RagSummarizeService.__new__(RagSummarizeService)
        service.evidence_threshold = 0.55
        service.reranker = None
        service.repository = type(
            "Repo",
            (),
            {"is_version_active": staticmethod(lambda version_id: True)},
        )()
        normalized = {
            "objects": ["沙发"],
            "intent": "maintenance",
            "required_terms": ["布艺"],
            "query_terms": ["布艺", "沙发", "清洗"],
        }
        candidate = RetrievalCandidate(
            chunk_id="c1",
            content="布艺沙发清洗前查看护理代码，使用中性清洁剂局部点擦。",
            metadata={"version_id": "v1", "category": "沙发"},
            bm25_score=12.0,
            fused_score=0.266,
            rerank_score=0.266,
            coverage={
                "object": True,
                "intent": True,
                "category": True,
                "required_terms": True,
                "lexical_hits": 3,
            },
        )

        enough, reason = service._judge_evidence(normalized, [candidate])

        self.assertTrue(enough)
        self.assertIsNone(reason)

    def test_bm25_fallback_rejects_cross_category_candidate(self):
        service = RagSummarizeService.__new__(RagSummarizeService)
        service.evidence_threshold = 0.55
        service.reranker = None
        service.repository = type(
            "Repo",
            (),
            {"is_version_active": staticmethod(lambda version_id: True)},
        )()
        normalized = {
            "objects": ["餐桌"],
            "intent": "repair",
            "required_terms": ["岩板"],
            "query_terms": ["岩板", "餐桌", "白印"],
        }
        candidate = RetrievalCandidate(
            chunk_id="c1",
            content="岩板茶几出现白印时联系售后评估。",
            metadata={"version_id": "v1", "category": "茶几"},
            bm25_score=8.0,
            fused_score=0.25,
            rerank_score=0.25,
            coverage={
                "object": False,
                "intent": True,
                "category": False,
                "required_terms": True,
                "lexical_hits": 2,
            },
        )

        enough, reason = service._judge_evidence(normalized, [candidate])

        self.assertFalse(enough)
        self.assertIn("object", reason)


if __name__ == "__main__":
    unittest.main()
