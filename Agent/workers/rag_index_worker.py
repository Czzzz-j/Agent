import os
import time

from db.knowledge_repository import KnowledgeRepository
from rag.document_parser import PARSER_VERSION, chunk_metadata, parse_document
from rag.vector_store import KnowledgeVectorStore
from utils.config_handler import chroma_conf, rag_conf
from utils.logger_handler import logger
from utils.path_tool import get_abs_path


class RagIndexWorker:
    def __init__(self):
        self.batch_size = int(os.getenv("RAG_WORKER_BATCH_SIZE", 20))
        self.poll_seconds = int(os.getenv("RAG_WORKER_POLL_SECONDS", 2))
        self.max_retries = int(os.getenv("RAG_WORKER_MAX_RETRIES", 8))
        self.stale_after_seconds = int(os.getenv("RAG_WORKER_STALE_AFTER_SECONDS", 300))
        self.source_root = get_abs_path(chroma_conf["data_path"])
        self.repository = KnowledgeRepository()
        self.vector_store = KnowledgeVectorStore(
            collection_name=chroma_conf["collection_name"],
            embedding_version=rag_conf["embedding_model_name"],
            require_persistent=True,
        )
        self.vector_store.healthcheck()
        self.repository.ensure_schema_state(chroma_conf["collection_name"])

    def run_forever(self):
        self.repository.requeue_stale_processing_tasks(self.stale_after_seconds)
        while True:
            processed = self.run_once()
            if not processed:
                time.sleep(self.poll_seconds)

    def run_once(self) -> int:
        self.vector_store.healthcheck()
        tasks = self.repository.claim_pending_tasks(self.batch_size)
        processed = 0
        for task in tasks:
            processed += 1
            try:
                self._process_task(task)
                self.repository.mark_task_completed(task["id"])
            except Exception as exc:
                logger.exception("[rag.worker] task failed: %s", task)
                self.repository.mark_task_failed(
                    task_id=task["id"],
                    retry_count=int(task.get("retry_count", 0)),
                    max_retries=self.max_retries,
                    error_message=str(exc),
                )
        self._refresh_consistent_health()
        return processed

    def _process_task(self, task: dict):
        if task["task_type"] == "delete":
            self._delete_document(task)
            return

        version = self.repository.load_version_for_task(task)
        if not version:
            raise RuntimeError(f"version not found for task {task['id']}")
        if task["task_type"] == "rebuild" and not self.repository.is_latest_version(
            version["document_id"],
            version["version_id"],
        ):
            logger.info(
                "[rag.worker] skip stale rebuild task_id=%s version_id=%s",
                task["id"],
                version["version_id"],
            )
            return

        source_path = version.get("source_snapshot_path") or version.get("source_path")
        absolute_path = os.path.join(self.source_root, source_path)
        if not os.path.exists(absolute_path):
            raise FileNotFoundError(absolute_path)

        chunks = parse_document(absolute_path)
        if not chunks:
            raise RuntimeError(f"no chunks parsed from {absolute_path}")

        metadata_rows = chunk_metadata(
            document_id=version["document_id"],
            version_id=version["version_id"],
            source_name=version["source_name"],
            domain=version["domain"],
            category=version.get("category", "general"),
            embedding_version=version.get("embedding_version", rag_conf["embedding_model_name"]),
            chunks=chunks,
        )
        self.repository.replace_version_chunks(
            document_id=version["document_id"],
            version_id=version["version_id"],
            chunks=chunks,
            metadata_rows=metadata_rows,
        )
        self.vector_store.upsert_chunks(chunks, metadata_rows)
        self._validate_index_write(version["version_id"], metadata_rows)
        old_version_id = self.repository.activate_version(version["document_id"], version["version_id"])
        if old_version_id and old_version_id != version["version_id"]:
            self.vector_store.delete_version(old_version_id)

    def _delete_document(self, task: dict):
        document_id = task["document_id"]
        self.vector_store.delete_document(document_id)
        self.repository.finalize_delete(document_id)

    def _validate_index_write(self, version_id: str, metadata_rows: list[dict]):
        payload = self.vector_store.fetch_by_ids([row["chunk_id"] for row in metadata_rows])
        ids = payload.get("ids", [])
        metadatas = payload.get("metadatas", [])
        if len(ids) != len(metadata_rows):
            raise RuntimeError(f"indexed chunk count mismatch for version {version_id}")
        for metadata in metadatas:
            if not metadata or metadata.get("version_id") != version_id:
                raise RuntimeError(f"indexed metadata mismatch for version {version_id}")

    def _refresh_consistent_health(self):
        mysql_count = self.repository.get_active_chunk_count()
        vector_count = self.vector_store.count()
        status = None if mysql_count == vector_count else "degraded"
        if status:
            logger.error(
                "[rag.worker] index count mismatch mysql=%s chroma=%s",
                mysql_count,
                vector_count,
            )
        return self.repository.refresh_health(
            chroma_conf["collection_name"],
            status=status,
        )


def main():
    RagIndexWorker().run_forever()


if __name__ == "__main__":
    main()
