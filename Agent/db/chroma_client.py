from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any

from utils.config_handler import chroma_conf

try:
    import chromadb  # type: ignore
except Exception:  # pragma: no cover - fallback for local evaluation environments
    chromadb = None


@dataclass
class _MemoryRecord:
    id: str
    document: str
    embedding: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


class _MemoryCollection:
    def __init__(self, name: str, metadata: dict[str, Any] | None = None) -> None:
        self.name = name
        self.metadata = metadata or {}
        self._records: dict[str, _MemoryRecord] = {}

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        for vector_id, document, embedding, metadata in zip(
            ids, documents, embeddings, metadatas
        ):
            self._records[vector_id] = _MemoryRecord(
                id=vector_id,
                document=document,
                embedding=[float(value) for value in embedding],
                metadata=dict(metadata or {}),
            )

    def delete(self, *, where: dict[str, Any] | None = None) -> None:
        if not where:
            self._records.clear()
            return
        to_delete = [
            record_id
            for record_id, record in self._records.items()
            if _matches_where(record.metadata, where)
        ]
        for record_id in to_delete:
            self._records.pop(record_id, None)

    def query(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int,
        where: dict[str, Any] | None = None,
        include: list[str] | None = None,
    ) -> dict[str, list[list[Any]]]:
        include = include or []
        query_embedding = [float(value) for value in query_embeddings[0]] if query_embeddings else []
        scored = []
        for record in self._records.values():
            if where and not _matches_where(record.metadata, where):
                continue
            scored.append((self._distance(query_embedding, record.embedding), record))
        scored.sort(key=lambda item: item[0])
        top = scored[:n_results]
        payload: dict[str, list[list[Any]]] = {"ids": [[record.id for _, record in top]]}
        if "documents" in include:
            payload["documents"] = [[record.document for _, record in top]]
        if "metadatas" in include:
            payload["metadatas"] = [[record.metadata for _, record in top]]
        if "distances" in include:
            payload["distances"] = [[distance for distance, _ in top]]
        return payload

    def get(self, *, ids: list[str], include: list[str] | None = None) -> dict[str, list[Any]]:
        include = include or []
        found = [self._records[vector_id] for vector_id in ids if vector_id in self._records]
        payload: dict[str, list[Any]] = {"ids": [record.id for record in found]}
        if "documents" in include:
            payload["documents"] = [record.document for record in found]
        if "metadatas" in include:
            payload["metadatas"] = [record.metadata for record in found]
        return payload

    def count(self) -> int:
        return len(self._records)

    @staticmethod
    def _distance(left: list[float], right: list[float]) -> float:
        if not left or not right:
            return 1.0
        size = min(len(left), len(right))
        dot = sum(left[index] * right[index] for index in range(size))
        left_norm = math.sqrt(sum(value * value for value in left[:size])) or 1.0
        right_norm = math.sqrt(sum(value * value for value in right[:size])) or 1.0
        similarity = dot / (left_norm * right_norm)
        return 1.0 - similarity


class _MemoryChromaClient:
    def __init__(self) -> None:
        self._collections: dict[str, _MemoryCollection] = {}

    def get_or_create_collection(
        self,
        *,
        name: str,
        metadata: dict[str, Any] | None = None,
    ) -> _MemoryCollection:
        collection = self._collections.get(name)
        if collection is None:
            collection = _MemoryCollection(name=name, metadata=metadata)
            self._collections[name] = collection
        return collection

    @staticmethod
    def heartbeat() -> int:
        return 1


class ChromaUnavailableError(RuntimeError):
    pass


def _matches_where(metadata: dict[str, Any], where: dict[str, Any]) -> bool:
    if not where:
        return True
    if "$and" in where:
        return all(_matches_where(metadata, clause) for clause in where["$and"])
    if "$or" in where:
        return any(_matches_where(metadata, clause) for clause in where["$or"])
    for key, condition in where.items():
        value = metadata.get(key)
        if isinstance(condition, dict):
            if "$eq" in condition and value != condition["$eq"]:
                return False
            if "$in" in condition and value not in condition["$in"]:
                return False
        elif value != condition:
            return False
    return True


def get_chroma_client(*, require_persistent: bool = False):
    mode = os.getenv("CHROMA_MODE", "auto").strip().lower()
    host = os.getenv("CHROMA_HOST") or chroma_conf.get("host")
    try:
        if chromadb is not None and host and mode != "persistent":
            port = int(os.getenv("CHROMA_PORT", chroma_conf.get("port", 8000)))
            client = chromadb.HttpClient(host=host, port=port)
            client.heartbeat()
            return client
        if chromadb is not None and (not host or mode == "persistent"):
            persist_directory = os.getenv(
                "CHROMA_PERSIST_DIR",
                chroma_conf.get("persist_directory", "./chroma_data"),
            )
            client = chromadb.PersistentClient(path=persist_directory)
            client.heartbeat()
            return client
    except Exception as exc:
        raise ChromaUnavailableError(f"Chroma is unavailable: {exc}") from exc

    allow_memory = os.getenv("CHROMA_ALLOW_MEMORY_FALLBACK", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if allow_memory and not require_persistent:
        return _MemoryChromaClient()
    raise ChromaUnavailableError(
        "chromadb is unavailable; set CHROMA_ALLOW_MEMORY_FALLBACK=true only for tests"
    )
