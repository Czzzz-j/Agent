import json
import os
import uuid
from datetime import datetime, timedelta

from db.mysql_client import MySQLClient
from utils.config_handler import chroma_conf
from utils.logger_handler import logger


class KnowledgeRepository:
    def __init__(self, mysql_pool=None):
        self.mysql_pool = mysql_pool or MySQLClient.get_pool()

    def ensure_schema_state(self, collection_name: str):
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO knowledge_index_state (
                    state_key, collection_name, generation, health_status, active_documents, active_chunks
                ) VALUES (%s, %s, 1, 'empty', 0, 0)
                ON DUPLICATE KEY UPDATE collection_name = VALUES(collection_name)
                """,
                ("knowledge_rag", collection_name),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def sync_source_files(
        self,
        source_root: str,
        file_paths: list[str],
        parser_version: str,
        embedding_version: str,
    ) -> dict[str, int]:
        existing_documents = self.get_documents_by_source_path()
        seen_paths = set()
        created = 0
        skipped = 0
        deleted = 0

        for file_path in file_paths:
            relative_path = os.path.relpath(file_path, source_root).replace("\\", "/")
            seen_paths.add(relative_path)
            changed = self._register_or_update_document(
                source_path=relative_path,
                absolute_path=file_path,
                parser_version=parser_version,
                embedding_version=embedding_version,
            )
            if changed:
                created += 1
            else:
                skipped += 1

        for relative_path, doc in existing_documents.items():
            if relative_path in seen_paths or doc["is_deleted"]:
                continue
            self.mark_document_deleted(doc["document_id"], relative_path)
            deleted += 1

        self.bump_last_sync(collection_name=chroma_conf["collection_name"])
        return {"created_or_changed": created, "skipped": skipped, "deleted": deleted}

    def rebuild_all(self, source_root: str, parser_version: str, embedding_version: str) -> int:
        documents = self.get_documents_by_source_path(include_deleted=False)
        count = 0
        for relative_path, document in documents.items():
            absolute_path = os.path.join(source_root, relative_path)
            if not os.path.exists(absolute_path):
                logger.warning(f"[rag.rebuild] missing source file: {absolute_path}")
                continue
            self._register_or_update_document(
                source_path=relative_path,
                absolute_path=absolute_path,
                parser_version=parser_version,
                embedding_version=embedding_version,
                force=True,
            )
            count += 1
        self.bump_last_sync(collection_name=chroma_conf["collection_name"])
        return count

    def get_documents_by_source_path(self, include_deleted: bool = True) -> dict[str, dict]:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            query = """
                SELECT document_id, source_path, active_version_id, is_deleted
                FROM knowledge_documents
            """
            if not include_deleted:
                query += " WHERE is_deleted = 0"
            cursor.execute(query)
            rows = cursor.fetchall() or []
            return {row["source_path"]: row for row in rows}
        finally:
            cursor.close()
            conn.close()

    def _register_or_update_document(
        self,
        source_path: str,
        absolute_path: str,
        parser_version: str,
        embedding_version: str,
        force: bool = False,
    ) -> bool:
        from utils.file_handler import get_file_sha256_hex
        from rag.document_parser import infer_domain_and_category

        content_sha256 = get_file_sha256_hex(absolute_path)
        if not content_sha256:
            raise RuntimeError(f"failed to hash file: {absolute_path}")

        source_name = os.path.basename(source_path)
        file_type = os.path.splitext(source_name)[1].lstrip(".").lower()
        domain, category = infer_domain_and_category(source_name)

        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            conn.start_transaction()
            cursor.execute(
                """
                SELECT document_id, active_version_id, is_deleted
                FROM knowledge_documents
                WHERE source_path = %s
                FOR UPDATE
                """,
                (source_path,),
            )
            document = cursor.fetchone()

            if not document:
                document_id = str(uuid.uuid4())
                cursor.execute(
                    """
                    INSERT INTO knowledge_documents (
                        document_id, source_name, source_path, file_type, domain, is_deleted
                    ) VALUES (%s, %s, %s, %s, %s, 0)
                    """,
                    (document_id, source_name, source_path, file_type, domain),
                )
                version_no = 1
            else:
                document_id = document["document_id"]
                cursor.execute(
                    """
                    SELECT version_id, content_sha256
                    FROM knowledge_document_versions
                    WHERE document_id = %s
                    ORDER BY version_no DESC
                    LIMIT 1
                    """,
                    (document_id,),
                )
                latest_version = cursor.fetchone()
                if latest_version and latest_version["content_sha256"] == content_sha256 and not force:
                    cursor.execute(
                        """
                        UPDATE knowledge_documents
                        SET source_name = %s, file_type = %s, domain = %s, is_deleted = 0
                        WHERE document_id = %s
                        """,
                        (source_name, file_type, domain, document_id),
                    )
                    conn.commit()
                    return False

                cursor.execute(
                    "SELECT COALESCE(MAX(version_no), 0) AS max_version_no FROM knowledge_document_versions WHERE document_id = %s",
                    (document_id,),
                )
                version_no = (cursor.fetchone() or {}).get("max_version_no", 0) + 1
                cursor.execute(
                    """
                    UPDATE knowledge_documents
                    SET source_name = %s, file_type = %s, domain = %s, is_deleted = 0
                    WHERE document_id = %s
                    """,
                    (source_name, file_type, domain, document_id),
                )

            version_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO knowledge_document_versions (
                    version_id, document_id, version_no, content_sha256, category,
                    parser_version, embedding_version, chunk_count, status, source_snapshot_path
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 'pending', %s)
                """,
                (
                    version_id,
                    document_id,
                    version_no,
                    content_sha256,
                    category,
                    parser_version,
                    embedding_version,
                    source_path,
                ),
            )
            aggregate_id = f"{document_id}:{version_id}"
            cursor.execute(
                """
                INSERT INTO knowledge_index_outbox (
                    task_type, aggregate_id, document_id, version_id, source_path, status
                ) VALUES (%s, %s, %s, %s, %s, 'pending')
                ON DUPLICATE KEY UPDATE
                    status = 'pending',
                    retry_count = 0,
                    next_retry_at = CURRENT_TIMESTAMP,
                    locked_at = NULL,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                ("upsert", aggregate_id, document_id, version_id, source_path),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def mark_document_deleted(self, document_id: str, source_path: str):
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            conn.start_transaction()
            cursor.execute(
                "UPDATE knowledge_documents SET is_deleted = 1 WHERE document_id = %s",
                (document_id,),
            )
            cursor.execute(
                """
                INSERT INTO knowledge_index_outbox (
                    task_type, aggregate_id, document_id, source_path, status
                ) VALUES (%s, %s, %s, %s, 'pending')
                ON DUPLICATE KEY UPDATE
                    status = 'pending',
                    retry_count = 0,
                    next_retry_at = CURRENT_TIMESTAMP,
                    locked_at = NULL,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                ("delete", f"{document_id}:delete", document_id, source_path),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def enqueue_rebuild(self) -> int:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT d.document_id, d.source_path, v.version_id
                FROM knowledge_documents d
                JOIN knowledge_document_versions v
                  ON v.document_id = d.document_id
                JOIN (
                    SELECT document_id, MAX(version_no) AS latest_version_no
                    FROM knowledge_document_versions
                    WHERE status <> 'deleted'
                    GROUP BY document_id
                ) latest
                  ON latest.document_id = v.document_id
                 AND latest.latest_version_no = v.version_no
                WHERE d.is_deleted = 0
                """
            )
            rows = cursor.fetchall() or []
            for row in rows:
                aggregate_id = f"{row['document_id']}:{row['version_id']}:rebuild"
                cursor.execute(
                    """
                    INSERT INTO knowledge_index_outbox (
                        task_type, aggregate_id, document_id, version_id, source_path, status
                    ) VALUES (%s, %s, %s, %s, %s, 'pending')
                    ON DUPLICATE KEY UPDATE
                        status = 'pending',
                        retry_count = 0,
                        next_retry_at = CURRENT_TIMESTAMP,
                        locked_at = NULL,
                        last_error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        "rebuild",
                        aggregate_id,
                        row["document_id"],
                        row["version_id"],
                        row["source_path"],
                    ),
                )
            conn.commit()
            return len(rows)
        finally:
            cursor.close()
            conn.close()

    def claim_pending_tasks(self, limit: int) -> list[dict]:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            conn.start_transaction()
            cursor.execute(
                f"""
                SELECT id, task_type, aggregate_id, document_id, version_id, source_path, retry_count
                FROM knowledge_index_outbox
                WHERE status = 'pending' AND next_retry_at <= CURRENT_TIMESTAMP
                ORDER BY id
                LIMIT {int(limit)}
                FOR UPDATE SKIP LOCKED
                """
            )
            tasks = cursor.fetchall() or []
            if not tasks:
                conn.commit()
                return []

            task_ids = [task["id"] for task in tasks]
            format_strings = ",".join(["%s"] * len(task_ids))
            cursor.execute(
                f"""
                UPDATE knowledge_index_outbox
                SET status = 'processing', locked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({format_strings})
                """,
                tuple(task_ids),
            )
            conn.commit()
            return tasks
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def requeue_stale_processing_tasks(self, stale_after_seconds: int):
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE knowledge_index_outbox
                SET status = 'pending', locked_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE status = 'processing'
                  AND locked_at IS NOT NULL
                  AND locked_at < %s
                """,
                (datetime.utcnow() - timedelta(seconds=stale_after_seconds),),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def load_version_for_task(self, task: dict) -> dict | None:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            if task["version_id"]:
                cursor.execute(
                    """
                    SELECT d.document_id, d.source_name, d.source_path, d.domain, d.active_version_id,
                           v.version_id, v.version_no, v.content_sha256, v.category, v.parser_version,
                           v.embedding_version, v.source_snapshot_path
                    FROM knowledge_document_versions v
                    JOIN knowledge_documents d ON d.document_id = v.document_id
                    WHERE v.version_id = %s
                    """,
                    (task["version_id"],),
                )
                return cursor.fetchone()

            cursor.execute(
                """
                SELECT document_id, source_name, source_path, domain, active_version_id
                FROM knowledge_documents
                WHERE document_id = %s
                """,
                (task["document_id"],),
            )
            return cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

    def is_latest_version(self, document_id: str, version_id: str) -> bool:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT version_id
                FROM knowledge_document_versions
                WHERE document_id = %s AND status <> 'deleted'
                ORDER BY version_no DESC
                LIMIT 1
                """,
                (document_id,),
            )
            row = cursor.fetchone()
            return bool(row and row[0] == version_id)
        finally:
            cursor.close()
            conn.close()

    def replace_version_chunks(
        self,
        document_id: str,
        version_id: str,
        chunks: list[dict],
        metadata_rows: list[dict],
    ):
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            conn.start_transaction()
            cursor.execute(
                "DELETE FROM knowledge_chunks WHERE version_id = %s",
                (version_id,),
            )
            for chunk, metadata in zip(chunks, metadata_rows):
                cursor.execute(
                    """
                    INSERT INTO knowledge_chunks (
                        chunk_id, document_id, version_id, chunk_index, content,
                        keywords_json, metadata_json, content_hash, is_active
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)
                    """,
                    (
                        metadata["chunk_id"],
                        document_id,
                        version_id,
                        metadata["chunk_index"],
                        chunk["content"],
                        json.dumps(chunk.get("keywords", []), ensure_ascii=False),
                        json.dumps(metadata, ensure_ascii=False),
                        metadata["content_hash"],
                    ),
                )
            cursor.execute(
                """
                UPDATE knowledge_document_versions
                SET chunk_count = %s, status = 'processing'
                WHERE version_id = %s
                """,
                (len(chunks), version_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def activate_version(self, document_id: str, version_id: str):
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            conn.start_transaction()
            cursor.execute(
                "SELECT active_version_id FROM knowledge_documents WHERE document_id = %s FOR UPDATE",
                (document_id,),
            )
            row = cursor.fetchone() or {}
            old_version_id = row.get("active_version_id")

            cursor.execute(
                "UPDATE knowledge_chunks SET is_active = 0 WHERE document_id = %s",
                (document_id,),
            )
            cursor.execute(
                "UPDATE knowledge_chunks SET is_active = 1 WHERE version_id = %s",
                (version_id,),
            )
            cursor.execute(
                """
                UPDATE knowledge_document_versions
                SET status = CASE
                    WHEN version_id = %s THEN 'active'
                    WHEN document_id = %s AND status <> 'deleted' THEN 'superseded'
                    ELSE status
                END
                WHERE document_id = %s
                """,
                (version_id, document_id, document_id),
            )
            cursor.execute(
                """
                UPDATE knowledge_documents
                SET active_version_id = %s, is_deleted = 0
                WHERE document_id = %s
                """,
                (version_id, document_id),
            )
            conn.commit()
            return old_version_id
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def finalize_delete(self, document_id: str):
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            conn.start_transaction()
            cursor.execute(
                "UPDATE knowledge_chunks SET is_active = 0 WHERE document_id = %s",
                (document_id,),
            )
            cursor.execute(
                """
                UPDATE knowledge_document_versions
                SET status = 'deleted'
                WHERE document_id = %s
                """,
                (document_id,),
            )
            cursor.execute(
                """
                UPDATE knowledge_documents
                SET active_version_id = NULL, is_deleted = 1
                WHERE document_id = %s
                """,
                (document_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def mark_task_completed(self, task_id: int):
        self._finish_task(task_id, "completed")

    def mark_task_failed(self, task_id: int, retry_count: int, max_retries: int, error_message: str):
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            next_retry_seconds = min(2 ** max(retry_count, 0), 300)
            new_status = "dead" if retry_count + 1 >= max_retries else "pending"
            cursor.execute(
                """
                UPDATE knowledge_index_outbox
                SET status = %s,
                    retry_count = retry_count + 1,
                    next_retry_at = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s SECOND),
                    locked_at = NULL,
                    last_error = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (new_status, next_retry_seconds, error_message[:4000], task_id),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def _finish_task(self, task_id: int, status: str):
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE knowledge_index_outbox
                SET status = %s, locked_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (status, task_id),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def get_active_chunks(self, allowed_domains: list[str] | None = None) -> list[dict]:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            params: list[Any] = []
            domain_clause = ""
            if allowed_domains:
                allowed_domains = [domain for domain in allowed_domains if domain]
                if not allowed_domains:
                    return []
                placeholders = ", ".join(["%s"] * len(allowed_domains))
                domain_clause = f" AND kd.domain IN ({placeholders})"
                params.extend(allowed_domains)
            cursor.execute(
                """
                SELECT kc.chunk_id, kc.content, kc.keywords_json, kc.metadata_json,
                       kd.source_name, kd.domain, kv.category, kv.embedding_version
                FROM knowledge_chunks kc
                JOIN knowledge_documents kd ON kd.document_id = kc.document_id
                JOIN knowledge_document_versions kv ON kv.version_id = kc.version_id
                WHERE kc.is_active = 1 AND kd.is_deleted = 0
                """ + domain_clause + """
                ORDER BY kd.source_name, kc.chunk_index
                """,
                tuple(params),
            )
            rows = cursor.fetchall() or []
            for row in rows:
                row["keywords"] = json.loads(row["keywords_json"]) if row["keywords_json"] else []
                row["metadata"] = json.loads(row["metadata_json"])
            return rows
        finally:
            cursor.close()
            conn.close()

    def is_version_active(self, version_id: str) -> bool:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT 1
                FROM knowledge_document_versions
                WHERE version_id = %s AND status = 'active'
                """,
                (version_id,),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()
            conn.close()

    def refresh_health(self, collection_name: str, status: str | None = None):
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM knowledge_documents WHERE is_deleted = 0 AND active_version_id IS NOT NULL"
            )
            active_documents = (cursor.fetchone() or {}).get("count", 0)
            cursor.execute(
                "SELECT COUNT(*) AS count FROM knowledge_chunks WHERE is_active = 1"
            )
            active_chunks = (cursor.fetchone() or {}).get("count", 0)
            cursor.execute(
                "SELECT COUNT(*) AS count FROM knowledge_index_outbox WHERE status = 'pending'"
            )
            pending_tasks = (cursor.fetchone() or {}).get("count", 0)
            cursor.execute(
                "SELECT COUNT(*) AS count FROM knowledge_index_outbox WHERE status = 'dead'"
            )
            dead_tasks = (cursor.fetchone() or {}).get("count", 0)

            if status is None:
                if dead_tasks > 0:
                    status = "degraded"
                elif active_chunks == 0:
                    status = "empty"
                elif pending_tasks > 0:
                    status = "stale"
                else:
                    status = "ready"

            cursor.execute(
                """
                INSERT INTO knowledge_index_state (
                    state_key, collection_name, generation, health_status, active_documents, active_chunks, last_sync_at
                ) VALUES (%s, %s, 1, %s, %s, %s, CURRENT_TIMESTAMP)
                ON DUPLICATE KEY UPDATE
                    collection_name = VALUES(collection_name),
                    generation = generation + 1,
                    health_status = VALUES(health_status),
                    active_documents = VALUES(active_documents),
                    active_chunks = VALUES(active_chunks),
                    updated_at = CURRENT_TIMESTAMP
                """,
                ("knowledge_rag", collection_name, status, active_documents, active_chunks),
            )
            conn.commit()
            return {
                "status": status,
                "active_documents": active_documents,
                "active_chunks": active_chunks,
                "pending_tasks": pending_tasks,
                "dead_tasks": dead_tasks,
            }
        finally:
            cursor.close()
            conn.close()

    def get_health(self, collection_name: str) -> dict:
        return self.refresh_health(collection_name)

    def get_active_chunk_count(self) -> int:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE is_active = 1")
            row = cursor.fetchone()
            return int(row[0] if row else 0)
        finally:
            cursor.close()
            conn.close()

    def bump_last_sync(self, collection_name: str):
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO knowledge_index_state (
                    state_key, collection_name, generation, health_status, active_documents, active_chunks, last_sync_at
                ) VALUES (%s, %s, 1, 'empty', 0, 0, CURRENT_TIMESTAMP)
                ON DUPLICATE KEY UPDATE last_sync_at = CURRENT_TIMESTAMP
                """,
                ("knowledge_rag", collection_name),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
