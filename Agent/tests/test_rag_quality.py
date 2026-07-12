import unittest
from unittest.mock import patch

from agent.multi_agent import AnswerComposer, MultiAgentRouter
from db.chroma_client import ChromaUnavailableError, get_chroma_client
from evaluation.build_golden_dataset import build_cases


class RagQualityTest(unittest.TestCase):
    def test_golden_dataset_has_required_category_counts(self):
        cases = build_cases()

        self.assertEqual(len(cases), 200)
        self.assertEqual(sum(case["category"] == "rag" for case in cases), 80)
        self.assertEqual(sum(case["category"] == "routing" for case in cases), 40)
        self.assertEqual(sum(case["category"] == "memory" for case in cases), 30)
        self.assertEqual(sum(case["category"] == "tools" for case in cases), 25)
        self.assertEqual(sum(case["category"] == "security" for case in cases), 25)

    def test_greeting_does_not_inherit_furniture_route_from_context(self):
        router = MultiAgentRouter()

        routed = router._route_by_rules(
            query="你好，在吗？",
            task_route={"task": {"topic": "沙发清洁", "goal": "去除污渍"}},
            history_recall_context="之前一直在讨论沙发",
            system_context="Current task: 沙发保养",
        )

        self.assertEqual(
            [route["agent_name"] for route in routed["specialist_routes"]],
            [],
        )

    def test_furniture_fault_word_does_not_add_device_agent(self):
        router = MultiAgentRouter()

        routed = router._route_by_rules(
            query="沙发出现 E999 故障码是什么意思？",
            task_route={},
            history_recall_context="",
            system_context="",
        )

        self.assertEqual(
            [route["agent_name"] for route in routed["specialist_routes"]],
            ["KnowledgeAgent"],
        )
        self.assertEqual(routed["specialist_routes"][0]["domain"], "furniture")

    def test_device_route_uses_knowledge_agent_robot_domain(self):
        router = MultiAgentRouter()

        routed = router._route_by_rules(
            query="扫地机器人滚刷卡住了怎么办？",
            task_route={},
            history_recall_context="",
            system_context="",
        )

        self.assertEqual(
            [route["agent_name"] for route in routed["specialist_routes"]],
            ["KnowledgeAgent"],
        )
        self.assertEqual(routed["specialist_routes"][0]["domain"], "robot_vacuum")

    def test_furniture_and_device_route_uses_mixed_knowledge_domain(self):
        router = MultiAgentRouter()

        routed = router._route_by_rules(
            query="扫地机器人清洁效率下降是不是和地毯有关？",
            task_route={},
            history_recall_context="",
            system_context="",
        )

        self.assertEqual(
            [route["agent_name"] for route in routed["specialist_routes"]],
            ["KnowledgeAgent"],
        )
        self.assertEqual(routed["specialist_routes"][0]["domain"], "mixed")

    def test_single_specialist_answer_is_not_rewritten(self):
        composer = AnswerComposer.__new__(AnswerComposer)
        answer, conflicts = composer.compose(
            query="布艺沙发怎么清洗？",
            task_route={},
            system_context="",
            history_recall_context="",
            recent_history=[],
            specialist_results=[
                {
                    "agent_name": "KnowledgeAgent",
                    "summary": "先查看洗涤标签，再用中性清洁剂局部点擦。",
                    "confidence": 0.9,
                    "evidence": ["沙发 FAQ"],
                    "covered_points": ["清洗"],
                    "unresolved_points": [],
                    "status": "answered",
                    "refusal_reason": None,
                }
            ],
        )

        self.assertEqual(answer, "先查看洗涤标签，再用中性清洁剂局部点擦。")
        self.assertEqual(conflicts, [])

    def test_memory_chroma_requires_explicit_test_flag(self):
        with patch("db.chroma_client.chromadb", None), patch.dict(
            "os.environ",
            {"CHROMA_ALLOW_MEMORY_FALLBACK": ""},
            clear=False,
        ):
            with self.assertRaises(ChromaUnavailableError):
                get_chroma_client()

        with patch("db.chroma_client.chromadb", None), patch.dict(
            "os.environ",
            {"CHROMA_ALLOW_MEMORY_FALLBACK": "true"},
            clear=False,
        ):
            client = get_chroma_client()
            self.assertEqual(client.heartbeat(), 1)


if __name__ == "__main__":
    unittest.main()
