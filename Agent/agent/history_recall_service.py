import re
from typing import Any

from langchain_core.documents import Document

from agent.conversation_vector_store import ConversationVectorStore
from db.session_repository import SessionPersistenceError, SessionRepository
from utils.logger_handler import logger


SEMANTIC_RECALL_MARKERS = [
    "之前",
    "以前",
    "上次",
    "曾经",
    "聊过",
    "提过",
    "说过",
    "问过",
    "建议过",
    "还记得",
    "历史上",
]
RECENT_CONTEXT_MARKERS = ["刚才", "刚刚", "上面", "这个", "那个"]
PROFILE_RECALL_MARKERS = ["我家有什么", "我的设备", "我的家具", "我的偏好", "我的预算"]


class HistoryRecallService:
    def __init__(
        self,
        session_repository: SessionRepository,
        vector_store: ConversationVectorStore | None = None,
    ):
        self.session_repository = session_repository
        self._vector_store = vector_store
        self._reranker: Any | None = None
        self._reranker_initialized = False

    def build_recall_context(
        self,
        query: str,
        session_id: str,
        user_uuid: str,
    ) -> str:
        exact_position = self._extract_exact_position(query)
        if exact_position is not None:
            try:
                return self._build_exact_recall(
                    session_id=session_id,
                    user_uuid=user_uuid,
                    position=exact_position,
                )
            except SessionPersistenceError:
                raise
            except Exception as exc:
                logger.warning(
                    "[history recall] exact lookup failed for session_id=%s: %s",
                    session_id,
                    exc,
                )
                return ""

        if not self._is_semantic_recall_query(query):
            return ""

        try:
            candidates = self._get_vector_store().search(
                query=query,
                user_uuid=user_uuid,
                limit=8,
            )
            return self._build_semantic_recall(
                query=query,
                current_session_id=session_id,
                candidates=candidates,
                user_uuid=user_uuid,
            )
        except SessionPersistenceError:
            raise
        except Exception as exc:
            logger.warning(
                "[history recall] semantic retrieval failed for user_uuid=%s: %s",
                user_uuid,
                exc,
            )
            return ""

    def _build_exact_recall(
        self,
        session_id: str,
        user_uuid: str,
        position: int,
    ) -> str:
        message = self.session_repository.get_nth_user_message(
            session_id=session_id,
            user_uuid=user_uuid,
            position=position,
        )
        if not message:
            return (
                "Historical conversation recall (exact MySQL lookup):\n"
                f"- No stored user message exists at position {position} "
                "in the current session."
            )
        return (
            "Historical conversation recall (exact MySQL lookup):\n"
            f"- User message #{position}: {self._trim(message['content'], 500)}\n"
            f"- Source message id: {message['id']}"
        )

    def _build_semantic_recall(
        self,
        query: str,
        current_session_id: str,
        candidates: list[dict[str, Any]],
        user_uuid: str,
    ) -> str:
        if not candidates:
            return ""

        candidates.sort(
            key=lambda item: (
                item["distance"]
                - (0.08 if item["metadata"].get("session_id") == current_session_id else 0)
            )
        )
        candidates = self._rerank(query, candidates)[:3]
        references = [
            (
                str(candidate["metadata"].get("session_id", "")),
                str(candidate["metadata"].get("request_id", "")),
            )
            for candidate in candidates
            if candidate["metadata"].get("session_id")
            and candidate["metadata"].get("request_id")
        ]
        verified = self.session_repository.get_verified_conversation_episodes(
            user_uuid=user_uuid,
            references=references,
        )

        lines = ["Historical conversation recall (semantic candidates verified in MySQL):"]
        for candidate in candidates:
            metadata = candidate["metadata"]
            key = (
                str(metadata.get("session_id", "")),
                str(metadata.get("request_id", "")),
            )
            episode = verified.get(key)
            if not episode:
                continue
            session_label = (
                "current session"
                if episode["session_id"] == current_session_id
                else "earlier session"
            )
            lines.extend(
                [
                    f"- Source: {session_label}, message id {episode['user_message_id']}",
                    f"  User: {self._trim(episode['user_message'], 300)}",
                    f"  Assistant: {self._trim(episode['assistant_message'], 500)}",
                ]
            )

        return "\n".join(lines) if len(lines) > 1 else ""

    def _rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        reranker = self._get_reranker()
        if reranker is None:
            return candidates

        documents = [
            Document(page_content=candidate["document"], metadata={"candidate": candidate})
            for candidate in candidates
        ]
        try:
            reranked = reranker.rerank(query, documents, top_k=len(documents))
            return [document.metadata["candidate"] for document in reranked]
        except Exception as exc:
            logger.warning("[history recall] reranking failed: %s", exc)
            return candidates

    def _get_vector_store(self) -> ConversationVectorStore:
        if self._vector_store is None:
            self._vector_store = ConversationVectorStore()
        return self._vector_store

    def _get_reranker(self):
        if self._reranker_initialized:
            return self._reranker
        self._reranker_initialized = True
        try:
            from rag.reranker import BGEReranker

            self._reranker = BGEReranker()
        except Exception as exc:
            logger.info("[history recall] reranker unavailable: %s", exc)
            self._reranker = None
        return self._reranker

    @staticmethod
    def _extract_exact_position(query: str) -> int | None:
        if "第一" in query and any(marker in query for marker in ["问", "说", "提"]):
            return 1

        match = re.search(r"第\s*(\d+)\s*轮.{0,12}(?:问|说|提)", query)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _is_semantic_recall_query(query: str) -> bool:
        return any(marker in query for marker in SEMANTIC_RECALL_MARKERS)

    @staticmethod
    def _should_use_existing_memory(query: str) -> bool:
        if any(marker in query for marker in PROFILE_RECALL_MARKERS):
            return True
        return any(marker in query for marker in RECENT_CONTEXT_MARKERS) and not any(
            marker in query for marker in SEMANTIC_RECALL_MARKERS
        )

    @staticmethod
    def _trim(text: str, limit: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."
