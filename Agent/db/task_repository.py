import json
import uuid
from typing import Any

from db.mysql_client import MySQLClient


TASK_STATUSES = {"active", "paused", "resolved", "abandoned"}
FACT_TYPES = {
    "confirmed_fact",
    "constraint",
    "attempt",
    "result",
    "rejection",
    "open_question",
}


class TaskConflictError(RuntimeError):
    """Raised when a task was concurrently updated."""


class TaskRepository:
    def __init__(self):
        self.mysql_pool = MySQLClient.get_pool()

    def list_user_tasks(
        self,
        user_uuid: str,
        statuses: tuple[str, ...] = ("active", "paused", "resolved"),
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        placeholders = ", ".join(["%s"] * len(statuses))
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT task_id, user_uuid, origin_session_id, active_session_id,
                       origin_request_id,
                       topic, subject_type, status, goal, next_action,
                       state_version, last_message_id, created_at, updated_at
                FROM conversation_tasks
                WHERE user_uuid = %s
                  AND status IN ({placeholders})
                ORDER BY
                    CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    updated_at DESC,
                    id DESC
                LIMIT %s
                """,
                (user_uuid, *statuses, limit),
            )
            return cursor.fetchall()
        finally:
            cursor.close()
            conn.close()

    def get_active_task(
        self,
        user_uuid: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT task_id, user_uuid, origin_session_id, active_session_id,
                       origin_request_id,
                       topic, subject_type, status, goal, next_action,
                       state_version, last_message_id, created_at, updated_at
                FROM conversation_tasks
                WHERE user_uuid = %s
                  AND active_session_id = %s
                  AND status = 'active'
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (user_uuid, session_id),
            )
            return cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

    def get_task_with_facts(
        self,
        task_id: str,
        user_uuid: str,
    ) -> dict[str, Any] | None:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT task_id, user_uuid, origin_session_id, active_session_id,
                       origin_request_id,
                       topic, subject_type, status, goal, next_action,
                       state_version, last_message_id, created_at, updated_at
                FROM conversation_tasks
                WHERE task_id = %s AND user_uuid = %s
                """,
                (task_id, user_uuid),
            )
            task = cursor.fetchone()
            if not task:
                return None

            cursor.execute(
                """
                SELECT id, fact_type, fact_value_json, confidence,
                       source_message_id, request_id, created_at, updated_at
                FROM conversation_task_facts
                WHERE task_id = %s AND status = 'active'
                ORDER BY updated_at DESC, id DESC
                """,
                (task_id,),
            )
            facts: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                try:
                    value = json.loads(row["fact_value_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                facts.append({**row, "value": value})
            task["facts"] = facts
            return task
        finally:
            cursor.close()
            conn.close()

    def has_task_event(self, task_id: str, request_id: str) -> bool:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT 1
                FROM conversation_task_events
                WHERE task_id = %s AND request_id = %s
                LIMIT 1
                """,
                (task_id, request_id),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()
            conn.close()

    def create_task(
        self,
        user_uuid: str,
        session_id: str,
        topic: str,
        subject_type: str | None,
        goal: str,
        last_message_id: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            conn.start_transaction()
            if request_id:
                existing_task = self._get_task_by_origin_request(cursor, user_uuid, request_id)
                if existing_task:
                    conn.commit()
                    return self.get_task_with_facts(existing_task["task_id"], user_uuid) or existing_task

            self._pause_active_tasks(cursor, user_uuid, session_id)
            task_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO conversation_tasks (
                    task_id, user_uuid, origin_session_id, active_session_id,
                    origin_request_id,
                    topic, subject_type, status, goal, last_message_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)
                """,
                (
                    task_id,
                    user_uuid,
                    session_id,
                    session_id,
                    request_id,
                    topic,
                    subject_type,
                    goal,
                    last_message_id,
                ),
            )
            if request_id:
                existing_task = self._get_task_by_origin_request(cursor, user_uuid, request_id)
                if existing_task:
                    task_id = existing_task["task_id"]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
        return self.get_task_with_facts(task_id, user_uuid) or {}

    def activate_task(
        self,
        task_id: str,
        user_uuid: str,
        session_id: str,
        last_message_id: int,
    ) -> dict[str, Any] | None:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            conn.start_transaction()
            self._pause_active_tasks(cursor, user_uuid, session_id, except_task_id=task_id)
            cursor.execute(
                """
                UPDATE conversation_tasks
                SET status = 'active',
                    active_session_id = %s,
                    last_message_id = GREATEST(last_message_id, %s),
                    state_version = state_version + 1
                WHERE task_id = %s AND user_uuid = %s
                """,
                (session_id, last_message_id, task_id, user_uuid),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
        return self.get_task_with_facts(task_id, user_uuid)

    def apply_patch(
        self,
        task_id: str,
        user_uuid: str,
        expected_version: int,
        request_id: str,
        source_message_id: int,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            conn.start_transaction()
            cursor.execute(
                """
                INSERT IGNORE INTO conversation_task_events (
                    task_id, request_id, source_message_id, patch_json
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    task_id,
                    request_id,
                    source_message_id,
                    json.dumps(patch, ensure_ascii=False),
                ),
            )
            if cursor.rowcount == 0:
                conn.commit()
            else:
                updates = patch.get("task_updates", {})
                status = updates.get("status")
                if status not in TASK_STATUSES:
                    status = None

                cursor.execute(
                    """
                    UPDATE conversation_tasks
                    SET topic = COALESCE(%s, topic),
                        subject_type = COALESCE(%s, subject_type),
                        goal = COALESCE(%s, goal),
                        next_action = COALESCE(%s, next_action),
                        status = COALESCE(%s, status),
                        last_message_id = GREATEST(last_message_id, %s),
                        state_version = state_version + 1
                    WHERE task_id = %s
                      AND user_uuid = %s
                      AND state_version = %s
                    """,
                    (
                        self._clean_text(updates.get("topic"), 200),
                        self._clean_text(updates.get("subject_type"), 64),
                        self._clean_text(updates.get("goal"), 1000),
                        self._clean_text(updates.get("next_action"), 1000),
                        status,
                        source_message_id,
                        task_id,
                        user_uuid,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TaskConflictError(f"Task {task_id} was updated concurrently")

                for fact in patch.get("facts", []):
                    fact_type = fact.get("fact_type")
                    if fact_type not in FACT_TYPES:
                        continue
                    value = fact.get("value")
                    if value in (None, "", [], {}):
                        continue
                    cursor.execute(
                        """
                        INSERT INTO conversation_task_facts (
                            task_id, fact_type, fact_value_json, confidence,
                            source_message_id, request_id
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            fact_value_json = VALUES(fact_value_json),
                            confidence = VALUES(confidence),
                            source_message_id = VALUES(source_message_id),
                            status = 'active'
                        """,
                        (
                            task_id,
                            fact_type,
                            json.dumps(value, ensure_ascii=False),
                            self._confidence(fact.get("confidence")),
                            source_message_id,
                            request_id,
                        ),
                    )
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
        return self.get_task_with_facts(task_id, user_uuid)

    def _pause_active_tasks(
        self,
        cursor: Any,
        user_uuid: str,
        session_id: str,
        except_task_id: str | None = None,
    ) -> None:
        query = """
            UPDATE conversation_tasks
            SET status = 'paused', state_version = state_version + 1
            WHERE user_uuid = %s
              AND active_session_id = %s
              AND status = 'active'
        """
        params: list[Any] = [user_uuid, session_id]
        if except_task_id:
            query += " AND task_id <> %s"
            params.append(except_task_id)
        cursor.execute(query, tuple(params))

    def _get_task_by_origin_request(
        self,
        cursor: Any,
        user_uuid: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        cursor.execute(
            """
            SELECT task_id
            FROM conversation_tasks
            WHERE user_uuid = %s
              AND origin_request_id = %s
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (user_uuid, request_id),
        )
        return cursor.fetchone()

    @staticmethod
    def _clean_text(value: Any, limit: int) -> str | None:
        if not isinstance(value, str):
            return None
        value = " ".join(value.split()).strip()
        return value[:limit] if value else None

    @staticmethod
    def _confidence(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.90
