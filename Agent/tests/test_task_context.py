import unittest
from unittest.mock import patch

from agent.context_assembler import ContextAssembler
from agent.task_service import TaskService


SOFA_TASK = {
    "task_id": "task-sofa",
    "user_uuid": "user-1",
    "origin_session_id": "session-1",
    "active_session_id": "session-1",
    "topic": "布艺沙发异味",
    "subject_type": "sofa",
    "status": "paused",
    "goal": "去除异味并保证宠物安全",
    "next_action": "检查海绵填充层是否受潮",
    "state_version": 3,
    "last_message_id": 20,
    "facts": [
        {
            "fact_type": "constraint",
            "value": ["家里有猫", "不使用刺激性强的清洁剂"],
            "source_message_id": 12,
        },
        {
            "fact_type": "attempt",
            "value": ["通风三天"],
            "source_message_id": 18,
        },
    ],
}

ROBOT_TASK = {
    "task_id": "task-robot",
    "user_uuid": "user-1",
    "origin_session_id": "session-1",
    "active_session_id": "session-1",
    "topic": "扫地机器人滚刷故障",
    "subject_type": "robot_vacuum",
    "status": "active",
    "goal": "解决滚刷卡住",
    "next_action": "拆下滚刷检查缠绕物",
    "state_version": 2,
    "last_message_id": 50,
    "facts": [],
}


class FakeTaskRepository:
    def get_active_task(self, user_uuid, session_id):
        return ROBOT_TASK

    def list_user_tasks(self, user_uuid):
        return [ROBOT_TASK, SOFA_TASK]

    def get_task_with_facts(self, task_id, user_uuid):
        if task_id == "task-sofa":
            return SOFA_TASK
        return ROBOT_TASK


class FakeSessionRepository:
    def get_recent_history(self, session_id, user_uuid):
        return [
            {"role": "user", "content": f"noise-{index}"}
            for index in range(10)
        ]


class FakeMemoryRepository:
    def get_user_memory_map(self, user_uuid):
        return {
            "home_environment": {
                "memory_value": ["家里有猫", "住在南方潮湿地区"]
            },
            "budget_preference": {
                "memory_value": ["扫地机器人预算三千元"]
            },
        }

    def get_session_memory(self, session_id):
        return None


class TaskRoutingTest(unittest.TestCase):
    def setUp(self):
        self.service = TaskService(FakeTaskRepository())

    def test_old_sofa_task_is_resumed_after_unrelated_turns(self):
        route = self.service.route(
            query="那个沙发的海绵确实有点潮，接下来怎么办？",
            session_id="session-1",
            user_uuid="user-1",
        )

        self.assertEqual(route["action"], "resume")
        self.assertEqual(route["task"]["task_id"], "task-sofa")

    def test_small_talk_does_not_bind_to_task(self):
        route = self.service.route(
            query="谢谢",
            session_id="session-1",
            user_uuid="user-1",
        )

        self.assertEqual(route["action"], "no_task")

    def test_same_subject_with_different_intent_creates_new_task(self):
        with patch.object(self.service, "_semantic_rank", return_value=[]):
            route = self.service.route(
                query="扫地机器人预算三千，应该怎么选？",
                session_id="session-1",
                user_uuid="user-1",
            )

        self.assertEqual(route["action"], "new")
        self.assertEqual(route["draft"]["subject_type"], "robot_vacuum")


class ContextAssemblerTest(unittest.TestCase):
    def test_context_contains_only_selected_task_and_three_recent_turns(self):
        assembler = ContextAssembler(
            session_repository=FakeSessionRepository(),
            memory_repository=FakeMemoryRepository(),
            recent_turns=3,
        )
        result = assembler.assemble(
            query="沙发海绵受潮怎么办",
            session_id="session-1",
            user_uuid="user-1",
            route={"action": "resume", "task": SOFA_TASK},
        )

        self.assertIn("布艺沙发异味", result["system_context"])
        self.assertNotIn("扫地机器人滚刷故障", result["system_context"])
        self.assertIn("家里有猫", result["system_context"])
        self.assertNotIn("预算三千元", result["system_context"])
        self.assertEqual(result["recent_history"], [])

    def test_continuing_current_task_keeps_three_recent_turns(self):
        assembler = ContextAssembler(
            session_repository=FakeSessionRepository(),
            memory_repository=FakeMemoryRepository(),
            recent_turns=3,
        )
        result = assembler.assemble(
            query="按你说的检查过了",
            session_id="session-1",
            user_uuid="user-1",
            route={"action": "continue", "task": ROBOT_TASK},
        )

        self.assertEqual(len(result["recent_history"]), 6)
        self.assertEqual(result["recent_history"][0]["content"], "noise-4")

    def test_exact_mysql_evidence_precedes_task_state(self):
        assembler = ContextAssembler(
            session_repository=FakeSessionRepository(),
            memory_repository=FakeMemoryRepository(),
        )
        result = assembler.assemble(
            query="我第一次问了什么",
            session_id="session-1",
            user_uuid="user-1",
            route={"action": "continue", "task": SOFA_TASK},
            history_recall_context=(
                "Historical conversation recall (exact MySQL lookup): first"
            ),
        )

        self.assertTrue(
            result["system_context"].startswith("<recalled_exact>"),
            f"expected <recalled_exact> tag first, got: {result['system_context'][:80]}",
        )


if __name__ == "__main__":
    unittest.main()
