import os
from datetime import datetime
from typing import Any

from db.chroma_client import get_chroma_client
from model.factory import embed_model


class ConversationVectorStore:
    def __init__(self):
        self.collection_name = os.getenv(
            "CONVERSATION_MEMORY_COLLECTION",
            "conversation_memory_v1",
        )
        self.embedding_version = os.getenv(
            "CONVERSATION_EMBEDDING_VERSION",
            "text-embedding-v4",
        )
        self.collection = get_chroma_client().get_or_create_collection(
            name=self.collection_name,
            metadata={"description": "Cross-session conversation memory index"},
        )

    def upsert_episode(self, episode: dict[str, Any]) -> str:
        vector_id = self.vector_id(episode["session_id"], episode["request_id"])
        document = self._episode_document(
            episode["user_message"],
            episode["assistant_message"],
        )
        embedding = embed_model.embed_query(document)
        self.collection.upsert(
            ids=[vector_id],
            documents=[document],
            embeddings=[embedding],
            metadatas=[
                {
                    "user_uuid": episode["user_uuid"],
                    "session_id": episode["session_id"],
                    "request_id": episode["request_id"],
                    "user_message_id": int(episode["user_message_id"]),
                    "assistant_message_id": int(episode["assistant_message_id"]),
                    "created_at": self._serialize_datetime(episode["created_at"]),
                    "embedding_version": self.embedding_version,
                    "memory_type": "conversation_episode",
                }
            ],
        )
        return vector_id

    def search(
        self,
        query: str,
        user_uuid: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        query_embedding = embed_model.embed_query(query)
        result = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=limit,
            where={
                "$and": [
                    {"user_uuid": {"$eq": user_uuid}},
                    {"memory_type": {"$eq": "conversation_episode"}},
                    {"embedding_version": {"$eq": self.embedding_version}},
                ]
            },
            include=["documents", "metadatas", "distances"],
        )

        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        return [
            {
                "id": vector_id,
                "document": document,
                "metadata": metadata or {},
                "distance": float(distance),
            }
            for vector_id, document, metadata, distance in zip(
                ids,
                documents,
                metadatas,
                distances,
            )
        ]

    @staticmethod
    def vector_id(session_id: str, request_id: str) -> str:
        return f"conversation_episode:{session_id}:{request_id}"

    @staticmethod
    def _episode_document(user_message: str, assistant_message: str) -> str:
        return (
            f"User question:\n{user_message.strip()}\n\n"
            f"Assistant answer:\n{assistant_message.strip()}"
        )

    @staticmethod
    def _serialize_datetime(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat(timespec="seconds")
        return str(value)
