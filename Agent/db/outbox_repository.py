from datetime import datetime, timedelta
from typing import Any

from db.mysql_client import MySQLClient


TASK_TYPE_CONVERSATION_EPISODE = "conversation_episode"


class OutboxRepository:
    def __init__(self):
        self.mysql_pool = MySQLClient.get_pool()

    @staticmethod
    def enqueue_conversation_episode(
        cursor: Any,
        session_id: str,
        user_uuid: str,
        request_id: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO memory_index_outbox (
                task_type, aggregate_id, session_id, user_uuid
            )
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE aggregate_id = VALUES(aggregate_id)
            """,
            (
                TASK_TYPE_CONVERSATION_EPISODE,
                f"{session_id}:{request_id}",
                session_id,
                user_uuid,
            ),
        )

    def recover_stale_tasks(self, stale_after_seconds: int) -> int:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE memory_index_outbox
                SET status = 'pending',
                    locked_at = NULL,
                    next_retry_at = CURRENT_TIMESTAMP,
                    last_error = CONCAT(
                        COALESCE(last_error, ''),
                        CASE WHEN last_error IS NULL OR last_error = '' THEN '' ELSE '\n' END,
                        'Recovered stale processing task'
                    )
                WHERE status = 'processing'
                  AND locked_at < DATE_SUB(CURRENT_TIMESTAMP, INTERVAL %s SECOND)
                """,
                (stale_after_seconds,),
            )
            recovered = cursor.rowcount
            conn.commit()
            return recovered
        finally:
            cursor.close()
            conn.close()

    def enqueue_missing_conversation_episodes(self) -> int:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO memory_index_outbox (
                    task_type, aggregate_id, session_id, user_uuid
                )
                SELECT
                    %s,
                    CONCAT(cm.session_id, ':', cm.request_id),
                    cm.session_id,
                    cs.user_uuid
                FROM chat_messages cm
                JOIN chat_sessions cs ON cs.session_id = cm.session_id
                WHERE cm.request_id IS NOT NULL
                  AND cm.role IN ('user', 'assistant')
                GROUP BY cm.session_id, cm.request_id, cs.user_uuid
                HAVING SUM(cm.role = 'user') > 0
                   AND SUM(cm.role = 'assistant') > 0
                ON DUPLICATE KEY UPDATE aggregate_id = VALUES(aggregate_id)
                """,
                (TASK_TYPE_CONVERSATION_EPISODE,),
            )
            inserted = cursor.rowcount
            conn.commit()
            return inserted
        finally:
            cursor.close()
            conn.close()

    def requeue_all_conversation_episodes(self) -> int:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE memory_index_outbox
                SET status = 'pending',
                    retry_count = 0,
                    next_retry_at = CURRENT_TIMESTAMP,
                    locked_at = NULL,
                    last_error = NULL
                WHERE task_type = %s
                """,
                (TASK_TYPE_CONVERSATION_EPISODE,),
            )
            requeued = cursor.rowcount
            conn.commit()
            return requeued
        finally:
            cursor.close()
            conn.close()

    def claim_pending_tasks(self, limit: int) -> list[dict[str, Any]]:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            conn.start_transaction()
            cursor.execute(
                """
                SELECT id, task_type, aggregate_id, session_id, user_uuid, retry_count
                FROM memory_index_outbox
                WHERE status = 'pending'
                  AND next_retry_at <= CURRENT_TIMESTAMP
                ORDER BY id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            tasks = cursor.fetchall()
            if tasks:
                task_ids = [int(task["id"]) for task in tasks]
                placeholders = ", ".join(["%s"] * len(task_ids))
                cursor.execute(
                    f"""
                    UPDATE memory_index_outbox
                    SET status = 'processing',
                        locked_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
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

    def get_conversation_episode(self, task: dict[str, Any]) -> dict[str, Any] | None:
        request_id = self._request_id_from_aggregate(task["aggregate_id"], task["session_id"])
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT
                    cm.id,
                    cm.role,
                    cm.content,
                    cm.created_at,
                    cs.user_uuid
                FROM chat_messages cm
                JOIN chat_sessions cs ON cs.session_id = cm.session_id
                WHERE cm.session_id = %s
                  AND cm.request_id = %s
                  AND cs.user_uuid = %s
                ORDER BY cm.sequence_no ASC, cm.id ASC
                """,
                (task["session_id"], request_id, task["user_uuid"]),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()

        user_message = next((row for row in rows if row["role"] == "user"), None)
        assistant_message = next((row for row in rows if row["role"] == "assistant"), None)
        if not user_message or not assistant_message:
            return None

        return {
            "session_id": task["session_id"],
            "user_uuid": task["user_uuid"],
            "request_id": request_id,
            "user_message_id": int(user_message["id"]),
            "assistant_message_id": int(assistant_message["id"]),
            "user_message": user_message["content"],
            "assistant_message": assistant_message["content"],
            "created_at": user_message["created_at"],
        }

    def mark_completed(self, task_id: int) -> None:
        self._update_task(
            """
            UPDATE memory_index_outbox
            SET status = 'completed',
                locked_at = NULL,
                last_error = NULL
            WHERE id = %s AND status = 'processing'
            """,
            (task_id,),
        )

    def mark_failed(
        self,
        task_id: int,
        retry_count: int,
        error: Exception,
        max_retries: int,
    ) -> None:
        next_retry_count = retry_count + 1
        error_text = str(error)[:4000]
        if next_retry_count >= max_retries:
            self._update_task(
                """
                UPDATE memory_index_outbox
                SET status = 'dead',
                    retry_count = %s,
                    locked_at = NULL,
                    last_error = %s
                WHERE id = %s
                """,
                (next_retry_count, error_text, task_id),
            )
            return

        delay_seconds = min(1800, 10 * (2 ** (next_retry_count - 1)))
        next_retry_at = datetime.now() + timedelta(seconds=delay_seconds)
        self._update_task(
            """
            UPDATE memory_index_outbox
            SET status = 'pending',
                retry_count = %s,
                next_retry_at = %s,
                locked_at = NULL,
                last_error = %s
            WHERE id = %s
            """,
            (next_retry_count, next_retry_at, error_text, task_id),
        )

    def _update_task(self, query: str, params: tuple[Any, ...]) -> None:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(query, params)
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    @staticmethod
    def _request_id_from_aggregate(aggregate_id: str, session_id: str) -> str:
        prefix = f"{session_id}:"
        if not aggregate_id.startswith(prefix):
            raise ValueError("Invalid conversation episode aggregate_id")
        return aggregate_id[len(prefix):]
