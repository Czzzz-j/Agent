import json
from typing import Any

from db.mysql_client import MySQLClient
from db.redis_client import get_redis_client
from utils.logger_handler import logger


class MemoryRepository:
    def __init__(self, session_summary_ttl: int = 3600, user_memory_ttl: int = 3600):
        self.mysql_pool = MySQLClient.get_pool()
        self.redis = get_redis_client()
        self.session_summary_ttl = session_summary_ttl
        self.user_memory_ttl = user_memory_ttl

    def get_session_memory(self, session_id: str) -> dict[str, Any] | None:
        cache_key = self._session_memory_cache_key(session_id)
        try:
            cached = self.redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception as exc:
            logger.warning("[memory cache] failed to read session memory for session_id=%s: %s", session_id, exc)

        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT session_id, summary_json, source_message_upto_id, updated_at
                FROM session_memory
                WHERE session_id = %s
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            try:
                summary = json.loads(row["summary_json"])
            except (TypeError, json.JSONDecodeError):
                logger.warning("[memory] invalid session summary json for session_id=%s", session_id)
                return None

            memory = {
                "session_id": row["session_id"],
                "summary": summary,
                "source_message_upto_id": row["source_message_upto_id"],
                "updated_at": str(row["updated_at"]),
            }
            self._cache_session_memory(session_id, memory)
            return memory
        finally:
            cursor.close()
            conn.close()

    def upsert_session_memory(self, session_id: str, summary: dict[str, Any], source_message_upto_id: int) -> None:
        summary_json = json.dumps(summary, ensure_ascii=False)
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO session_memory (session_id, summary_json, source_message_upto_id)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    summary_json = VALUES(summary_json),
                    source_message_upto_id = VALUES(source_message_upto_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (session_id, summary_json, source_message_upto_id),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

        self._cache_session_memory(
            session_id,
            {
                "session_id": session_id,
                "summary": summary,
                "source_message_upto_id": source_message_upto_id,
            },
        )

    def get_user_memory_map(self, user_uuid: str) -> dict[str, dict[str, Any]]:
        cache_key = self._user_memory_cache_key(user_uuid)
        try:
            cached = self.redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception as exc:
            logger.warning("[memory cache] failed to read user memory for user_uuid=%s: %s", user_uuid, exc)

        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT user_uuid, memory_key, memory_value, confidence, source_session_id, source_message_id, updated_at
                FROM user_memory
                WHERE user_uuid = %s
                ORDER BY updated_at DESC, id DESC
                """,
                (user_uuid,),
            )
            rows = cursor.fetchall()
            memory_map: dict[str, dict[str, Any]] = {}
            for row in rows:
                try:
                    memory_value = json.loads(row["memory_value"])
                except (TypeError, json.JSONDecodeError):
                    logger.warning(
                        "[memory] invalid user memory json for user_uuid=%s, memory_key=%s",
                        user_uuid,
                        row["memory_key"],
                    )
                    continue
                memory_map[row["memory_key"]] = {
                    "user_uuid": row["user_uuid"],
                    "memory_key": row["memory_key"],
                    "memory_value": memory_value,
                    "confidence": float(row["confidence"]),
                    "source_session_id": row["source_session_id"],
                    "source_message_id": row["source_message_id"],
                    "updated_at": str(row["updated_at"]),
                }
            self._cache_user_memory(user_uuid, memory_map)
            return memory_map
        finally:
            cursor.close()
            conn.close()

    def upsert_user_memory(
        self,
        user_uuid: str,
        memory_key: str,
        memory_value: list[str],
        confidence: float,
        source_session_id: str,
        source_message_id: int,
    ) -> None:
        value_json = json.dumps(memory_value, ensure_ascii=False)
        conn = self.mysql_pool.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO user_memory (
                    user_uuid, memory_key, memory_value, confidence, source_session_id, source_message_id
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    memory_value = VALUES(memory_value),
                    confidence = VALUES(confidence),
                    source_session_id = VALUES(source_session_id),
                    source_message_id = VALUES(source_message_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_uuid, memory_key, value_json, confidence, source_session_id, source_message_id),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

        try:
            self.redis.delete(self._user_memory_cache_key(user_uuid))
            self.redis.delete(self._user_memory_summary_cache_key(user_uuid))
        except Exception as exc:
            logger.warning("[memory cache] failed to invalidate user memory for user_uuid=%s: %s", user_uuid, exc)

    def get_user_memory_summary_text(self, user_uuid: str) -> str | None:
        try:
            cached = self.redis.get(self._user_memory_summary_cache_key(user_uuid))
            if cached:
                return cached.decode("utf-8") if isinstance(cached, bytes) else str(cached)
        except Exception as exc:
            logger.warning("[memory cache] failed to read user memory summary for user_uuid=%s: %s", user_uuid, exc)
        return None

    def cache_user_memory_summary_text(self, user_uuid: str, summary_text: str) -> None:
        try:
            self.redis.setex(self._user_memory_summary_cache_key(user_uuid), self.user_memory_ttl, summary_text)
        except Exception as exc:
            logger.warning("[memory cache] failed to cache user memory summary for user_uuid=%s: %s", user_uuid, exc)

    def _session_memory_cache_key(self, session_id: str) -> str:
        return f"session:{session_id}:memory_summary"

    def _user_memory_cache_key(self, user_uuid: str) -> str:
        return f"user:{user_uuid}:memory_map"

    def _user_memory_summary_cache_key(self, user_uuid: str) -> str:
        return f"user:{user_uuid}:memory_summary_text"

    def _cache_session_memory(self, session_id: str, memory: dict[str, Any]) -> None:
        try:
            self.redis.setex(
                self._session_memory_cache_key(session_id),
                self.session_summary_ttl,
                json.dumps(memory, ensure_ascii=False),
            )
        except Exception as exc:
            logger.warning("[memory cache] failed to cache session memory for session_id=%s: %s", session_id, exc)

    def _cache_user_memory(self, user_uuid: str, memory_map: dict[str, dict[str, Any]]) -> None:
        try:
            self.redis.setex(
                self._user_memory_cache_key(user_uuid),
                self.user_memory_ttl,
                json.dumps(memory_map, ensure_ascii=False),
            )
        except Exception as exc:
            logger.warning("[memory cache] failed to cache user memory for user_uuid=%s: %s", user_uuid, exc)
