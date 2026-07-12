from model.factory import embed_model

from db.chroma_client import get_chroma_client
from utils.config_handler import chroma_conf


class KnowledgeVectorStore:
    def __init__(
        self,
        collection_name: str | None = None,
        embedding_version: str = "text-embedding-v4",
        *,
        require_persistent: bool = False,
    ):
        self.collection_name = collection_name or chroma_conf["collection_name"]
        self.embedding_version = embedding_version
        self.client = get_chroma_client(require_persistent=require_persistent)
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"description": "Knowledge retrieval index"},
        )

    def upsert_chunks(self, chunks: list[dict], metadata_rows: list[dict]):
        ids = [row["chunk_id"] for row in metadata_rows]
        embeddings = [embed_model.embed_query(chunk["content"]) for chunk in chunks]
        documents = [chunk["content"] for chunk in chunks]
        self.collection.upsert(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadata_rows,
        )

    def delete_version(self, version_id: str):
        self.collection.delete(where={"version_id": {"$eq": version_id}})

    def delete_document(self, document_id: str):
        self.collection.delete(where={"document_id": {"$eq": document_id}})

    def query(self, query: str, top_k: int, where: dict | None = None) -> list[dict]:
        query_embedding = embed_model.embed_query(query)
        query_kwargs = {
            "query_embeddings": [query_embedding],
            "n_results": top_k,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            query_kwargs["where"] = where
        result = self.collection.query(**query_kwargs)
        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        return [
            {
                "chunk_id": chunk_id,
                "content": content,
                "metadata": metadata or {},
                "distance": float(distance),
            }
            for chunk_id, content, metadata, distance in zip(ids, documents, metadatas, distances)
        ]

    def fetch_by_ids(self, ids: list[str]) -> dict:
        return self.collection.get(ids=ids, include=["documents", "metadatas"])

    def count(self) -> int:
        return int(self.collection.count())

    def healthcheck(self) -> dict:
        heartbeat = self.client.heartbeat()
        return {
            "collection_name": self.collection_name,
            "vector_count": self.count(),
            "heartbeat": heartbeat,
        }
