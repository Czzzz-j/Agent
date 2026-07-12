import json
from typing import Any

from db.mysql_client import MySQLClient
from db.outbox_repository import OutboxRepository
from db.redis_client import get_redis_client
from utils.logger_handler import logger


class SessionPersistenceError(RuntimeError):
    """Raised when a chat turn cannot be durably persisted."""


class SessionRepository:
    def __init__(self, max_turns: int = 10, redis_ttl: int = 3600):
        self.max_turns = max_turns
        self.redis_ttl = redis_ttl
        self.redis = get_redis_client()
        self.mysql_pool = MySQLClient.get_pool()
        self.outbox_repository = OutboxRepository()

    def get_recent_history(
        self,
        session_id: str,
        user_uuid: str | None = None,
    ) -> list[dict[str, str]]:
        if user_uuid and not self._session_is_accessible(session_id, user_uuid):
            return []

        cached_history = self._load_recent_history_from_cache(session_id)
        if cached_history is not None:
            return cached_history

        history = self._load_recent_history_from_mysql(session_id)
        if history:
            self._refresh_cache(session_id, history)
        return history

    def get_session_messages(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            if limit is None:
                cursor.execute(
                    """
                    SELECT id, role, content, created_at, request_id, sequence_no
                    FROM chat_messages
                    WHERE session_id = %s
                    ORDER BY created_at ASC, id ASC
                    """,
                    (session_id,),
                )
                return cursor.fetchall()

            cursor.execute(
                """
                SELECT id, role, content, created_at, request_id, sequence_no FROM (
                    SELECT id, role, content, created_at, request_id, sequence_no
                    FROM chat_messages
                    WHERE session_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                ) AS recent_messages
                ORDER BY created_at ASC, id ASC
                """,
                (session_id, limit),
            )
            return cursor.fetchall()
        finally:
            cursor.close()
            conn.close()

    def get_session_message_stats(self, session_id: str) -> dict[str, int]:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT COUNT(*) AS message_count, COALESCE(MAX(id), 0) AS latest_message_id
                FROM chat_messages
                WHERE session_id = %s
                """,
                (session_id,),
            )
            row = cursor.fetchone() or {}
            return {
                "message_count": int(row.get("message_count", 0)),
                "latest_message_id": int(row.get("latest_message_id", 0)),
            }
        finally:
            cursor.close()
            conn.close()

    def count_messages_up_to(self, session_id: str, message_id: int) -> int:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM chat_messages
                WHERE session_id = %s AND id <= %s
                """,
                (session_id, message_id),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0
        finally:
            cursor.close()
            conn.close()

    def get_nth_user_message(
        self,
        session_id: str,
        user_uuid: str,
        position: int,
    ) -> dict[str, Any] | None:
        if position < 1:
            return None

        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT cm.id, cm.session_id, cm.request_id, cm.content, cm.created_at
                FROM chat_messages cm
                JOIN chat_sessions cs ON cs.session_id = cm.session_id
                WHERE cm.session_id = %s
                  AND cs.user_uuid = %s
                  AND cm.role = 'user'
                ORDER BY cm.created_at ASC, cm.id ASC
                LIMIT 1 OFFSET %s
                """,
                (session_id, user_uuid, position - 1),
            )
            return cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

    def get_verified_conversation_episodes(
        self,
        user_uuid: str,
        references: list[tuple[str, str]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        if not references:
            return {}

        unique_references = list(dict.fromkeys(references))
        conditions = " OR ".join(
            ["(cm.session_id = %s AND cm.request_id = %s)"] * len(unique_references)
        )
        params: list[Any] = [user_uuid]
        for session_id, request_id in unique_references:
            params.extend([session_id, request_id])

        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT
                    cm.id,
                    cm.session_id,
                    cm.request_id,
                    cm.role,
                    cm.content,
                    cm.created_at,
                    cm.sequence_no
                FROM chat_messages cm
                JOIN chat_sessions cs ON cs.session_id = cm.session_id
                WHERE cs.user_uuid = %s
                  AND ({conditions})
                ORDER BY cm.created_at ASC, cm.id ASC
                """,
                tuple(params),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()

        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["session_id"], row["request_id"])
            episode = grouped.setdefault(
                key,
                {
                    "session_id": row["session_id"],
                    "request_id": row["request_id"],
                    "created_at": row["created_at"],
                },
            )
            if row["role"] == "user":
                episode["user_message_id"] = int(row["id"])
                episode["user_message"] = row["content"]
            elif row["role"] == "assistant":
                episode["assistant_message_id"] = int(row["id"])
                episode["assistant_message"] = row["content"]

        return {
            key: episode
            for key, episode in grouped.items()
            if episode.get("user_message") and episode.get("assistant_message")
        }

    def persist_turn(
        self,
        session_id: str,
        user_uuid: str,
        request_id: str,
        user_message: str,
        assistant_message: str,
    ) -> dict[str, int]:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            conn.start_transaction()
            self._ensure_user_exists(cursor, user_uuid)
            self._ensure_session_exists(cursor, session_id, user_uuid)

            cursor.execute(
                """
                SELECT id, role FROM chat_messages
                WHERE session_id = %s AND request_id = %s
                ORDER BY sequence_no ASC
                """,
                (session_id, request_id),
            )
            existing_rows = cursor.fetchall()
            if existing_rows:
                if len(existing_rows) != 2:
                    raise SessionPersistenceError("检测到不完整的会话记录，请稍后重试。")
                OutboxRepository.enqueue_conversation_episode(
                    cursor=cursor,
                    session_id=session_id,
                    user_uuid=user_uuid,
                    request_id=request_id,
                )
                logger.info(
                    "[session persistence] skipped duplicate request_id=%s for session_id=%s",
                    request_id,
                    session_id,
                )
            else:
                cursor.executemany(
                    """
                    INSERT INTO chat_messages (session_id, role, content, request_id, sequence_no)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    [
                        (session_id, "user", user_message, request_id, 1),
                        (session_id, "assistant", assistant_message, request_id, 2),
                    ],
                )
                OutboxRepository.enqueue_conversation_episode(
                    cursor=cursor,
                    session_id=session_id,
                    user_uuid=user_uuid,
                    request_id=request_id,
                )

            cursor.execute(
                """
                SELECT id, role
                FROM chat_messages
                WHERE session_id = %s AND request_id = %s
                ORDER BY sequence_no ASC, id ASC
                """,
                (session_id, request_id),
            )
            persisted_rows = cursor.fetchall()
            message_ids = {
                f"{row['role']}_message_id": int(row["id"])
                for row in persisted_rows
                if row["role"] in {"user", "assistant"}
            }
            if set(message_ids) != {"user_message_id", "assistant_message_id"}:
                raise SessionPersistenceError("会话消息写入不完整，请稍后重试。")

            conn.commit()
            history = self._load_recent_history_from_mysql(session_id)
            self._refresh_cache(session_id, history)
            return message_ids
        except Exception as exc:
            conn.rollback()
            logger.error(
                "[session persistence] failed to persist turn for session_id=%s, request_id=%s: %s",
                session_id,
                request_id,
                exc,
                exc_info=True,
            )
            raise SessionPersistenceError("会话保存失败，请稍后重试。") from exc
        finally:
            cursor.close()
            conn.close()

    def _cache_key(self, session_id: str) -> str:
        return f"session:{session_id}:messages"

    def _session_is_accessible(self, session_id: str, user_uuid: str) -> bool:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT 1 FROM chat_sessions WHERE session_id = %s AND user_uuid = %s",
                (session_id, user_uuid),
            )
            row = cursor.fetchone()
            if row:
                return True

            cursor.execute(
                "SELECT 1 FROM chat_sessions WHERE session_id = %s",
                (session_id,),
            )
            if cursor.fetchone():
                raise SessionPersistenceError("会话不属于当前用户。")
            return False
        finally:
            cursor.close()
            conn.close()

    def _load_recent_history_from_cache(self, session_id: str) -> list[dict[str, str]] | None:
        try:
            messages_json = self.redis.lrange(self._cache_key(session_id), 0, -1)
        except Exception as exc:
            logger.warning(
                "[session cache] failed to read cache for session_id=%s: %s",
                session_id,
                exc,
            )
            return None

        if not messages_json:
            return None

        history: list[dict[str, str]] = []
        for raw_message in messages_json:
            try:
                history.append(json.loads(raw_message))
            except json.JSONDecodeError:
                logger.warning(
                    "[session cache] invalid cached message for session_id=%s, cache will be ignored",
                    session_id,
                )
                return None
        return history

    def _load_recent_history_from_mysql(self, session_id: str) -> list[dict[str, str]]:
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content, created_at
                    FROM chat_messages
                    WHERE session_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                ) AS recent_messages
                ORDER BY created_at ASC, id ASC
                """,
                (session_id, self.max_turns * 2),
            )
            rows = cursor.fetchall()
            return [{"role": row["role"], "content": row["content"]} for row in rows]
        finally:
            cursor.close()
            conn.close()

    def _refresh_cache(self, session_id: str, history: list[dict[str, str]]) -> None:
        try:
            payload = [json.dumps(message, ensure_ascii=False) for message in history[-self.max_turns * 2:]]
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.delete(self._cache_key(session_id))
            if payload:
                pipeline.rpush(self._cache_key(session_id), *payload)
            pipeline.expire(self._cache_key(session_id), self.redis_ttl)
            pipeline.execute()
        except Exception as exc:
            logger.warning(
                "[session cache] failed to refresh cache for session_id=%s: %s",
                session_id,
                exc,
            )

    def _ensure_user_exists(self, cursor: Any, user_uuid: str) -> None:
        cursor.execute(
            "INSERT IGNORE INTO users (uuid) VALUES (%s)",
            (user_uuid,),
        )

    def _ensure_session_exists(self, cursor: Any, session_id: str, user_uuid: str) -> None:
        cursor.execute(
            """
            INSERT INTO chat_sessions (session_id, user_uuid)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP
            """,
            (session_id, user_uuid),
        )
        cursor.execute(
            "SELECT user_uuid FROM chat_sessions WHERE session_id = %s",
            (session_id,),
        )
        row = cursor.fetchone()
        owner_uuid = row.get("user_uuid") if isinstance(row, dict) else row[0]
        if owner_uuid != user_uuid:
            raise SessionPersistenceError("会话不属于当前用户。")
