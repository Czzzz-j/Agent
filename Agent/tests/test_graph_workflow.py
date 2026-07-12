import unittest
from unittest.mock import MagicMock, patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

from agent.graph_workflow import AgentGraphWorkflow


class FakeSessionRepository:
    def __init__(self, events):
        self.events = events
        self.persist_calls = []

    def persist_turn(self, session_id, user_uuid, request_id, user_message, assistant_message):
        self.events.append("persist")
        self.persist_calls.append(
            {
                "session_id": session_id,
                "user_uuid": user_uuid,
                "request_id": request_id,
                "user_message": user_message,
                "assistant_message": assistant_message,
            }
        )
        return {"user_message_id": 101, "assistant_message_id": 102}


class FakeTaskService:
    def __init__(self, events):
        self.events = events
        self.route_calls = []
        self.update_calls = []

    def route(self, query, session_id, user_uuid):
        self.events.append("route")
        self.route_calls.append((query, session_id, user_uuid))
        return {
            "action": "new",
            "task": None,
            "confidence": 0.9,
            "draft": {
                "topic": "chair question",
                "subject_type": "chair",
                "goal": "choose a chair",
            },
        }

    def update_after_turn(self, **kwargs):
        self.events.append("task_update")
        self.update_calls.append(kwargs)
        return {"task_id": "task-1"}


class FakeHistoryRecallService:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def build_recall_context(self, query, session_id, user_uuid):
        self.events.append("recall")
        self.calls.append((query, session_id, user_uuid))
        return "History recall context"


class FakeContextAssembler:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def assemble(self, query, session_id, user_uuid, route, history_recall_context=""):
        self.events.append("assemble")
        self.calls.append(
            {
                "query": query,
                "session_id": session_id,
                "user_uuid": user_uuid,
                "route": route,
                "history_recall_context": history_recall_context,
            }
        )
        return {
            "system_context": "System context",
            "recent_history": [{"role": "assistant", "content": "cached reply"}],
        }


class FakeMemoryService:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def refresh_long_term_memory(self, session_id, user_uuid):
        self.events.append("memory_refresh")
        self.calls.append((session_id, user_uuid))

    def refresh_memories(self, session_id, user_uuid):
        self.events.append("memory_refresh")
        self.calls.append((session_id, user_uuid))


class FakeSpecialistRouter:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def route(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "specialist_routes": self.routes,
            "route_confidence": 0.91,
            "conflicts": [],
        }


class FakeSpecialistRunner:
    def __init__(self, events, results):
        self.events = events
        self.results = results
        self.calls = []

    def run(self, **kwargs):
        self.events.append("specialist_run")
        self.calls.append(kwargs)
        return self.results


class FakeAnswerComposer:
    def __init__(self, events, answer="composed answer"):
        self.events = events
        self.answer = answer
        self.calls = []

    def compose(self, **kwargs):
        self.events.append("compose")
        self.calls.append(kwargs)
        return self.answer, []


class FakeQueryRewriter:
    def __init__(self):
        self.calls = []

    def rewrite(self, query, task, recent_history=None):
        self.calls.append({"query": query, "task": task, "recent_history": recent_history})
        return f"{query} [rewritten]"


class GraphWorkflowTest(unittest.TestCase):
    def test_graph_streams_answer_and_runs_post_commit_side_effects(self):
        events: list[str] = []
        inner_agent = create_agent(
            model=FakeListChatModel(responses=["hello"]),
            system_prompt="base system",
            tools=[],
        )

        session_repository = FakeSessionRepository(events)
        task_service = FakeTaskService(events)
        history_recall_service = FakeHistoryRecallService(events)
        context_assembler = FakeContextAssembler(events)
        memory_service = FakeMemoryService(events)

        workflow = AgentGraphWorkflow(
            inner_agent=inner_agent,
            session_repository=session_repository,
            task_service=task_service,
            history_recall_service=history_recall_service,
            context_assembler=context_assembler,
            memory_service=memory_service,
            use_agentic_mode=False,
        )

        chunks = list(
            workflow.execute_stream(
                query="How should I choose a chair?",
                session_id="session-1",
                user_uuid="user-1",
                request_id="request-1",
            )
        )

        self.assertEqual("".join(chunks), "hello")
        self.assertEqual(session_repository.persist_calls[0]["assistant_message"], "hello")
        self.assertEqual(task_service.update_calls[0]["request_id"], "request-1")
        self.assertEqual(memory_service.calls[0], ("session-1", "user-1"))
        self.assertGreater(events.index("assemble"), events.index("route"))
        self.assertGreater(events.index("assemble"), events.index("recall"))
        self.assertGreater(events.index("persist"), events.index("assemble"))
        self.assertGreater(events.index("task_update"), events.index("persist"))
        self.assertGreater(events.index("memory_refresh"), events.index("persist"))

    def test_multi_agent_path_routes_and_composes(self):
        events: list[str] = []
        inner_agent = None
        session_repository = FakeSessionRepository(events)
        task_service = FakeTaskService(events)
        history_recall_service = FakeHistoryRecallService(events)
        context_assembler = FakeContextAssembler(events)
        memory_service = FakeMemoryService(events)
        specialist_routes = [
            {
                "agent_name": "KnowledgeAgent",
                "domain": "furniture",
                "reason": "knowledge_query:furniture",
                "confidence": 0.9,
            },
            {"agent_name": "ReportAgent", "reason": "report_query", "confidence": 0.8},
        ]
        specialist_results = [
            {
                "agent_name": "KnowledgeAgent",
                "domain": "furniture",
                "summary": "沙发应选择耐磨、易清洁的面料。",
                "confidence": 0.9,
                "evidence": ["evidence-1"],
                "covered_points": ["sofa"],
                "unresolved_points": [],
                "refusal_reason": None,
            },
            {
                "agent_name": "ReportAgent",
                "summary": "本月使用记录显示清洁效率良好。",
                "confidence": 0.8,
                "evidence": ["evidence-2"],
                "covered_points": ["usage_report"],
                "unresolved_points": [],
                "refusal_reason": None,
            },
        ]

        workflow = AgentGraphWorkflow(
            inner_agent=inner_agent,
            session_repository=session_repository,
            task_service=task_service,
            history_recall_service=history_recall_service,
            context_assembler=context_assembler,
            memory_service=memory_service,
            specialist_router=FakeSpecialistRouter(specialist_routes),
            specialist_runner=FakeSpecialistRunner(events, specialist_results),
            answer_composer=FakeAnswerComposer(events, answer="composed answer"),
            use_agentic_mode=False,
        )

        chunks = list(
            workflow.execute_stream(
                query="帮我看下沙发怎么选，并结合这个月的使用记录给建议",
                session_id="session-2",
                user_uuid="user-2",
                request_id="request-2",
            )
        )

        self.assertEqual("".join(chunks), "composed answer")
        self.assertEqual(session_repository.persist_calls[0]["assistant_message"], "composed answer")
        self.assertIn("specialist_run", events)
        self.assertIn("compose", events)
        self.assertGreater(events.index("assemble"), events.index("route"))
        self.assertGreater(events.index("assemble"), events.index("recall"))
        self.assertGreater(events.index("compose"), events.index("specialist_run"))
        self.assertGreater(events.index("persist"), events.index("compose"))


    @patch("agent.graph_workflow.chat_model")
    def test_default_workflow_uses_multi_agent_mode(self, mock_chat_model):
        events: list[str] = []
        session_repository = FakeSessionRepository(events)
        task_service = FakeTaskService(events)
        history_recall_service = FakeHistoryRecallService(events)
        context_assembler = FakeContextAssembler(events)
        memory_service = FakeMemoryService(events)
        specialist_routes = [
            {
                "agent_name": "KnowledgeAgent",
                "domain": "robot_vacuum",
                "reason": "knowledge_query:robot_vacuum",
                "confidence": 0.9,
            }
        ]
        specialist_results = [
            {
                "agent_name": "KnowledgeAgent",
                "domain": "robot_vacuum",
                "summary": "滚刷卡住时，先断电并清理缠绕物。",
                "confidence": 0.9,
                "evidence": ["robot evidence"],
                "covered_points": ["滚刷"],
                "unresolved_points": [],
                "status": "answered",
                "refusal_reason": None,
            }
        ]

        workflow = AgentGraphWorkflow(
            inner_agent=None,
            session_repository=session_repository,
            task_service=task_service,
            history_recall_service=history_recall_service,
            context_assembler=context_assembler,
            memory_service=memory_service,
            specialist_router=FakeSpecialistRouter(specialist_routes),
            specialist_runner=FakeSpecialistRunner(events, specialist_results),
            answer_composer=FakeAnswerComposer(events, answer="multi-agent answer"),
        )

        chunks = list(
            workflow.execute_stream(
                query="扫地机器人滚刷卡住了怎么办？",
                session_id="session-default",
                user_uuid="user-default",
                request_id="request-default",
            )
        )

        self.assertEqual("".join(chunks), "multi-agent answer")
        self.assertIn("specialist_run", events)
        self.assertIn("compose", events)
        mock_chat_model.bind_tools.assert_not_called()


    @patch("agent.graph_workflow.chat_model")
    def test_agentic_mode_produces_answer(self, mock_chat_model):
        fake_model = MagicMock()
        fake_model.bind_tools.return_value = fake_model
        fake_model.invoke.return_value = AIMessage(content="这是主 agent 的最终回答。")

        mock_chat_model.bind_tools.return_value = fake_model
        mock_chat_model.invoke = fake_model.invoke

        events: list[str] = []
        session_repository = FakeSessionRepository(events)
        task_service = FakeTaskService(events)
        history_recall_service = FakeHistoryRecallService(events)
        context_assembler = FakeContextAssembler(events)
        memory_service = FakeMemoryService(events)
        query_rewriter = FakeQueryRewriter()

        workflow = AgentGraphWorkflow(
            inner_agent=None,
            session_repository=session_repository,
            task_service=task_service,
            history_recall_service=history_recall_service,
            context_assembler=context_assembler,
            memory_service=memory_service,
            use_agentic_mode=True,
            query_rewriter=query_rewriter,
        )

        chunks = list(
            workflow.execute_stream(
                query="布艺沙发怎么清洁",
                session_id="session-agentic-1",
                user_uuid="user-agentic-1",
                request_id="request-agentic-1",
            )
        )

        self.assertEqual("".join(chunks), "这是主 agent 的最终回答。")
        self.assertEqual(
            session_repository.persist_calls[0]["assistant_message"],
            "这是主 agent 的最终回答。",
        )
        self.assertIn("assemble", events)
        self.assertIn("persist", events)
        self.assertIn("memory_refresh", events)

    @patch("agent.graph_workflow.chat_model")
    def test_agentic_mode_with_tool_calls(self, mock_chat_model):
        fake_model = MagicMock()
        fake_model.bind_tools.return_value = fake_model
        fake_model.invoke.side_effect = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_001",
                        "name": "search_knowledge_base",
                        "args": {"query": "布艺沙发清洁 [rewritten]", "domain": "furniture"},
                    }
                ],
            ),
            AIMessage(content="根据知识库查询结果，布艺沙发建议用中性清洁剂。"),
        ]

        mock_chat_model.bind_tools.return_value = fake_model
        mock_chat_model.invoke = fake_model.invoke

        events: list[str] = []
        session_repository = FakeSessionRepository(events)
        task_service = FakeTaskService(events)
        history_recall_service = FakeHistoryRecallService(events)
        context_assembler = FakeContextAssembler(events)
        memory_service = FakeMemoryService(events)
        query_rewriter = FakeQueryRewriter()

        workflow = AgentGraphWorkflow(
            inner_agent=None,
            session_repository=session_repository,
            task_service=task_service,
            history_recall_service=history_recall_service,
            context_assembler=context_assembler,
            memory_service=memory_service,
            use_agentic_mode=True,
            query_rewriter=query_rewriter,
        )

        chunks = list(
            workflow.execute_stream(
                query="布艺沙发怎么清洁",
                session_id="session-agentic-2",
                user_uuid="user-agentic-2",
                request_id="request-agentic-2",
            )
        )

        self.assertEqual("".join(chunks), "根据知识库查询结果，布艺沙发建议用中性清洁剂。")
        self.assertIn("persist", events)
        self.assertEqual(fake_model.invoke.call_count, 2)


    @patch("agent.graph_workflow.chat_model")
    def test_rewritten_query_is_used(self, mock_chat_model):
        fake_model = MagicMock()
        fake_model.bind_tools.return_value = fake_model
        fake_model.invoke.return_value = AIMessage(content="rewritten answer")

        mock_chat_model.bind_tools.return_value = fake_model
        mock_chat_model.invoke = fake_model.invoke

        events: list[str] = []
        session_repository = FakeSessionRepository(events)
        task_service = FakeTaskService(events)
        history_recall_service = FakeHistoryRecallService(events)
        context_assembler = FakeContextAssembler(events)
        memory_service = FakeMemoryService(events)
        query_rewriter = FakeQueryRewriter()

        workflow = AgentGraphWorkflow(
            inner_agent=None,
            session_repository=session_repository,
            task_service=task_service,
            history_recall_service=history_recall_service,
            context_assembler=context_assembler,
            memory_service=memory_service,
            use_agentic_mode=True,
            query_rewriter=query_rewriter,
        )

        chunks = list(
            workflow.execute_stream(
                query="它怎么清洁",
                session_id="session-rw-1",
                user_uuid="user-rw-1",
                request_id="request-rw-1",
            )
        )

        self.assertEqual("".join(chunks), "rewritten answer")
        self.assertEqual(len(query_rewriter.calls), 1)
        self.assertEqual(query_rewriter.calls[0]["query"], "它怎么清洁")
        self.assertIn("[rewritten]", query_rewriter.calls[0]["query"] + " [rewritten]")
        self.assertEqual(
            session_repository.persist_calls[0]["user_message"],
            "它怎么清洁",
        )


if __name__ == "__main__":
    unittest.main()
