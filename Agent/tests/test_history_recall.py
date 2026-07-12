import unittest

from agent.history_recall_service import HistoryRecallService


class FakeSessionRepository:
    def __init__(self):
        self.verified_user_uuid = None

    def get_nth_user_message(self, session_id, user_uuid, position):
        if position != 1:
            return None
        return {
            "id": 11,
            "content": "我的布艺沙发有异味，应该怎么处理？",
        }

    def get_verified_conversation_episodes(self, user_uuid, references):
        self.verified_user_uuid = user_uuid
        return {
            ("old-session", "old-request"): {
                "session_id": "old-session",
                "request_id": "old-request",
                "user_message_id": 21,
                "assistant_message_id": 22,
                "user_message": "之前聊过沙发异味。",
                "assistant_message": "建议先通风并检查异味来源。",
            }
        }


class FakeVectorStore:
    def __init__(self):
        self.searched_user_uuid = None

    def search(self, query, user_uuid, limit):
        self.searched_user_uuid = user_uuid
        return [
            {
                "id": "episode",
                "document": "沙发异味",
                "metadata": {
                    "session_id": "old-session",
                    "request_id": "old-request",
                },
                "distance": 0.1,
            }
        ]


class HistoryRecallServiceTest(unittest.TestCase):
    def setUp(self):
        self.repository = FakeSessionRepository()
        self.vector_store = FakeVectorStore()
        self.service = HistoryRecallService(
            session_repository=self.repository,
            vector_store=self.vector_store,
        )
        self.service._reranker_initialized = True
        self.service._reranker = None

    def test_exact_first_question_uses_mysql_message(self):
        context = self.service.build_recall_context(
            query="我第一次问的问题是什么？",
            session_id="current-session",
            user_uuid="user-1",
        )

        self.assertIn("exact MySQL lookup", context)
        self.assertIn("我的布艺沙发有异味", context)
        self.assertEqual(self.vector_store.searched_user_uuid, None)

    def test_semantic_recall_is_verified_for_same_user(self):
        context = self.service.build_recall_context(
            query="我之前是不是问过沙发异味？",
            session_id="current-session",
            user_uuid="user-1",
        )

        self.assertEqual(self.vector_store.searched_user_uuid, "user-1")
        self.assertEqual(self.repository.verified_user_uuid, "user-1")
        self.assertIn("verified in MySQL", context)
        self.assertIn("之前聊过沙发异味", context)

    def test_profile_question_does_not_use_vector_search(self):
        context = self.service.build_recall_context(
            query="我家有什么设备？",
            session_id="current-session",
            user_uuid="user-1",
        )

        self.assertEqual(context, "")
        self.assertEqual(self.vector_store.searched_user_uuid, None)

    def test_numeric_turn_is_parsed(self):
        self.assertEqual(
            HistoryRecallService._extract_exact_position("我第100轮问了什么？"),
            100,
        )


if __name__ == "__main__":
    unittest.main()
