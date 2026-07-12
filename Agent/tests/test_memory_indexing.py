import unittest
from unittest.mock import patch

from agent.conversation_vector_store import ConversationVectorStore
from db.outbox_repository import OutboxRepository
from db.session_repository import SessionPersistenceError, SessionRepository
from workers.memory_index_worker import MemoryIndexWorker


class FakeCursor:
    def __init__(self):
        self.query = None
        self.params = None

    def execute(self, query, params):
        self.query = query
        self.params = params


class FakeCollection:
    def __init__(self):
        self.payload = None

    def upsert(self, **kwargs):
        self.payload = kwargs


class FakeEmbedding:
    def embed_query(self, document):
        return [0.1, 0.2]


class FakePersistenceCursor:
    def __init__(self, events):
        self.events = events
        self.fetchall_count = 0

    def execute(self, query, params):
        self.events.append("execute")

    def executemany(self, query, params):
        self.events.append("messages")

    def fetchall(self):
        self.fetchall_count += 1
        if self.fetchall_count == 1:
            return []
        return [
            {"id": 10, "role": "user"},
            {"id": 11, "role": "assistant"},
        ]

    def close(self):
        self.events.append("cursor_close")


class FakePersistenceConnection:
    def __init__(self, events):
        self.events = events
        self.cursor_instance = FakePersistenceCursor(events)

    def cursor(self, dictionary=False):
        return self.cursor_instance

    def start_transaction(self):
        self.events.append("start")

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("connection_close")


class FakePersistencePool:
    def __init__(self, connection):
        self.connection = connection

    def get_connection(self):
        return self.connection


class FakeOutboxRepository:
    def __init__(self, fail=False):
        self.fail = fail
        self.completed = []
        self.failed = []

    def claim_pending_tasks(self, limit):
        return [
            {
                "id": 1,
                "task_type": "conversation_episode",
                "aggregate_id": "session-1:request-1",
                "session_id": "session-1",
                "user_uuid": "user-1",
                "retry_count": 0,
            }
        ]

    def get_conversation_episode(self, task):
        return {
            "session_id": "session-1",
            "user_uuid": "user-1",
            "request_id": "request-1",
            "user_message_id": 10,
            "assistant_message_id": 11,
            "user_message": "沙发怎么清洁？",
            "assistant_message": "先吸尘，再局部测试清洁剂。",
            "created_at": "2026-06-19 10:00:00",
        }

    def mark_completed(self, task_id):
        self.completed.append(task_id)

    def mark_failed(self, **kwargs):
        self.failed.append(kwargs)


class FakeVectorStore:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.episodes = []

    def upsert_episode(self, episode):
        if self.should_fail:
            raise RuntimeError("chroma unavailable")
        self.episodes.append(episode)


class MemoryIndexingTest(unittest.TestCase):
    def test_outbox_uses_stable_aggregate_id(self):
        cursor = FakeCursor()

        OutboxRepository.enqueue_conversation_episode(
            cursor=cursor,
            session_id="session-1",
            user_uuid="user-1",
            request_id="request-1",
        )

        self.assertIn("ON DUPLICATE KEY UPDATE", cursor.query)
        self.assertEqual(cursor.params[1], "session-1:request-1")

    def test_outbox_failure_rolls_back_message_transaction(self):
        events = []
        connection = FakePersistenceConnection(events)
        repository = SessionRepository.__new__(SessionRepository)
        repository.mysql_pool = FakePersistencePool(connection)
        repository._ensure_user_exists = lambda cursor, user_uuid: events.append("user")
        repository._ensure_session_exists = (
            lambda cursor, session_id, user_uuid: events.append("session")
        )
        repository._load_recent_history_from_mysql = lambda session_id: []
        repository._refresh_cache = lambda session_id, history: None

        with patch(
            "db.session_repository.OutboxRepository.enqueue_conversation_episode",
            side_effect=RuntimeError("outbox insert failed"),
        ):
            with self.assertRaises(SessionPersistenceError):
                repository.persist_turn(
                    session_id="session-1",
                    user_uuid="user-1",
                    request_id="request-1",
                    user_message="question",
                    assistant_message="answer",
                )

        self.assertIn("messages", events)
        self.assertIn("rollback", events)
        self.assertNotIn("commit", events)

    def test_messages_and_outbox_commit_together(self):
        events = []
        connection = FakePersistenceConnection(events)
        repository = SessionRepository.__new__(SessionRepository)
        repository.mysql_pool = FakePersistencePool(connection)
        repository._ensure_user_exists = lambda cursor, user_uuid: events.append("user")
        repository._ensure_session_exists = (
            lambda cursor, session_id, user_uuid: events.append("session")
        )
        repository._load_recent_history_from_mysql = lambda session_id: []
        repository._refresh_cache = lambda session_id, history: None

        def enqueue(**kwargs):
            events.append("outbox")

        with patch(
            "db.session_repository.OutboxRepository.enqueue_conversation_episode",
            side_effect=enqueue,
        ):
            repository.persist_turn(
                session_id="session-1",
                user_uuid="user-1",
                request_id="request-1",
                user_message="question",
                assistant_message="answer",
            )

        self.assertLess(events.index("messages"), events.index("outbox"))
        self.assertLess(events.index("outbox"), events.index("commit"))

    def test_vector_upsert_uses_stable_id_and_metadata(self):
        store = ConversationVectorStore.__new__(ConversationVectorStore)
        store.collection_name = "conversation_memory_v1"
        store.embedding_version = "text-embedding-v4"
        store.collection = FakeCollection()
        episode = {
            "session_id": "session-1",
            "user_uuid": "user-1",
            "request_id": "request-1",
            "user_message_id": 10,
            "assistant_message_id": 11,
            "user_message": "沙发怎么清洁？",
            "assistant_message": "先吸尘。",
            "created_at": "2026-06-19 10:00:00",
        }

        with patch(
            "agent.conversation_vector_store.embed_model",
            FakeEmbedding(),
        ):
            vector_id = store.upsert_episode(episode)

        self.assertEqual(
            vector_id,
            "conversation_episode:session-1:request-1",
        )
        self.assertEqual(store.collection.payload["ids"], [vector_id])
        metadata = store.collection.payload["metadatas"][0]
        self.assertEqual(metadata["user_uuid"], "user-1")
        self.assertEqual(metadata["user_message_id"], 10)

    def test_worker_marks_successful_task_completed(self):
        worker = MemoryIndexWorker.__new__(MemoryIndexWorker)
        worker.batch_size = 20
        worker.max_retries = 8
        worker.outbox_repository = FakeOutboxRepository()
        worker.vector_store = FakeVectorStore()

        processed = worker.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(worker.outbox_repository.completed, [1])
        self.assertEqual(len(worker.vector_store.episodes), 1)

    def test_worker_schedules_retry_when_chroma_fails(self):
        worker = MemoryIndexWorker.__new__(MemoryIndexWorker)
        worker.batch_size = 20
        worker.max_retries = 8
        worker.outbox_repository = FakeOutboxRepository()
        worker.vector_store = FakeVectorStore(should_fail=True)

        processed = worker.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(worker.outbox_repository.completed, [])
        self.assertEqual(len(worker.outbox_repository.failed), 1)
        self.assertEqual(worker.outbox_repository.failed[0]["task_id"], 1)


if __name__ == "__main__":
    unittest.main()
