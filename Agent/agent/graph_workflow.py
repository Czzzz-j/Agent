from __future__ import annotations

import operator
import time
import uuid
from typing import Any, Annotated

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph, add_messages
from typing_extensions import TypedDict

from agent.agentic_tools import AGENTIC_TOOLS, build_tool_map
from agent.context_assembler import ContextAssembler
from agent.history_recall_service import HistoryRecallService
from agent.memory_service import MemoryService
from agent.multi_agent import AnswerComposer, MultiAgentRouter, MultiAgentRunner
from agent.query_rewriter import QueryRewriter
from agent.task_service import TaskService
from db.session_repository import SessionPersistenceError, SessionRepository
from model.factory import chat_model
from utils.logger_handler import logger


def _merge_timings(
    left: dict[str, float] | None,
    right: dict[str, float] | None,
) -> dict[str, float]:
    merged: dict[str, float] = {}
    if left:
        merged.update(left)
    if right:
        merged.update(right)
    return merged


class AgentGraphState(TypedDict, total=False):
    query: str
    session_id: str
    user_uuid: str
    request_id: str
    task_route: dict[str, Any]
    history_recall_context: str
    system_context: str
    recent_history: list[dict[str, Any]]
    specialist_routes: list[dict[str, Any]]
    specialist_results: Annotated[list[dict[str, Any]], operator.add]
    route_confidence: float
    composition_result: dict[str, Any]
    conflicts: Annotated[list[str], operator.add]
    rewritten_query: str
    tool_call_count: int
    messages: Annotated[list[BaseMessage], add_messages]
    answer: str
    user_message_id: int
    assistant_message_id: int
    node_errors: Annotated[list[str], operator.add]
    node_timings: Annotated[dict[str, float], _merge_timings]


class AgentGraphWorkflow:
    def __init__(
        self,
        *,
        inner_agent: Any | None = None,
        session_repository: SessionRepository,
        task_service: TaskService,
        history_recall_service: HistoryRecallService,
        context_assembler: ContextAssembler,
        memory_service: MemoryService,
        specialist_router: MultiAgentRouter | None = None,
        specialist_runner: MultiAgentRunner | None = None,
        answer_composer: AnswerComposer | None = None,
        use_agentic_mode: bool = False,
        query_rewriter: QueryRewriter | None = None,
    ):
        self.inner_agent = inner_agent
        self.session_repository = session_repository
        self.task_service = task_service
        self.history_recall_service = history_recall_service
        self.context_assembler = context_assembler
        self.memory_service = memory_service
        self.use_agentic_mode = use_agentic_mode
        self.specialist_router = specialist_router or MultiAgentRouter()
        self.specialist_runner = specialist_runner
        self.answer_composer = answer_composer or AnswerComposer()
        self.query_rewriter = query_rewriter or QueryRewriter()
        self._tool_map = build_tool_map()
        self.compiled_graph = self._build_graph()

    def execute_stream(
        self,
        query: str,
        session_id: str,
        user_uuid: str,
        request_id: str | None = None,
    ):
        request_id = request_id or str(uuid.uuid4())
        initial_state: AgentGraphState = {
            "query": query,
            "session_id": session_id,
            "user_uuid": user_uuid,
            "request_id": request_id,
            "messages": [],
            "specialist_routes": [],
            "specialist_results": [],
            "conflicts": [],
            "rewritten_query": "",
            "tool_call_count": 0,
            "node_errors": [],
            "node_timings": {},
        }

        try:
            final_state = self.compiled_graph.invoke(
                initial_state,
                config={"recursion_limit": 50},
            )
        except Exception as exc:
            logger.error(
                "[agent graph] failed for session_id=%s, request_id=%s: %s",
                session_id,
                request_id,
                exc,
                exc_info=True,
            )
            raise

        answer = str(final_state.get("answer", "")).strip()
        if not answer:
            raise SessionPersistenceError("模型未返回有效回答，请稍后重试。")
        yield answer

    def _build_graph(self):
        graph = StateGraph(AgentGraphState)
        graph.add_node("route_task", self._route_task)
        graph.add_node("recall_history", self._recall_history)
        graph.add_node("assemble_context", self._assemble_context)
        graph.add_node("persist_turn", self._persist_turn)
        graph.add_node("update_task_state", self._update_task_state)
        graph.add_node("refresh_user_memory", self._refresh_user_memory)

        graph.add_edge(START, "route_task")
        graph.add_edge(START, "recall_history")
        graph.add_edge("route_task", "assemble_context")
        graph.add_edge("recall_history", "assemble_context")

        if self.use_agentic_mode:
            graph.add_node("rewrite_query", self._rewrite_query)
            graph.add_node("agentic_answer", self._agentic_answer)
            graph.add_edge("assemble_context", "rewrite_query")
            graph.add_edge("rewrite_query", "agentic_answer")
            graph.add_edge("agentic_answer", "persist_turn")
        else:
            graph.add_node("route_specialists", self._route_specialists)
            graph.add_node("dispatch_specialists", self._dispatch_specialists)
            graph.add_node("compose_answer", self._compose_answer)
            graph.add_edge("assemble_context", "route_specialists")
            graph.add_edge("route_specialists", "dispatch_specialists")
            graph.add_edge("dispatch_specialists", "compose_answer")
            graph.add_edge("compose_answer", "persist_turn")

        graph.add_edge("persist_turn", "update_task_state")
        graph.add_edge("persist_turn", "refresh_user_memory")
        graph.add_edge("update_task_state", END)
        graph.add_edge("refresh_user_memory", END)

        return graph.compile()

    def _route_task(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            route = self.task_service.route(
                query=state["query"],
                session_id=state["session_id"],
                user_uuid=state["user_uuid"],
            )
            return {
                "task_route": route,
                "node_timings": {"route_task": time.perf_counter() - started},
            }
        except SessionPersistenceError:
            raise
        except Exception as exc:
            logger.warning(
                "[agent graph] route_task degraded for session_id=%s, request_id=%s: %s",
                state["session_id"],
                state["request_id"],
                exc,
            )
            return {
                "task_route": {"action": "no_task", "task": None, "confidence": 0.0},
                "node_errors": [f"route_task: {exc}"],
                "node_timings": {"route_task": time.perf_counter() - started},
            }

    def _recall_history(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            context = self.history_recall_service.build_recall_context(
                query=state["query"],
                session_id=state["session_id"],
                user_uuid=state["user_uuid"],
            )
            return {
                "history_recall_context": context,
                "node_timings": {"recall_history": time.perf_counter() - started},
            }
        except SessionPersistenceError:
            raise
        except Exception as exc:
            logger.warning(
                "[agent graph] recall_history degraded for session_id=%s, request_id=%s: %s",
                state["session_id"],
                state["request_id"],
                exc,
            )
            return {
                "history_recall_context": "",
                "node_errors": [f"recall_history: {exc}"],
                "node_timings": {"recall_history": time.perf_counter() - started},
            }

    def _assemble_context(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        route = state.get("task_route") or {"action": "no_task", "task": None}
        history_recall_context = state.get("history_recall_context", "")
        try:
            assembled = self.context_assembler.assemble(
                query=state["query"],
                session_id=state["session_id"],
                user_uuid=state["user_uuid"],
                route=route,
                history_recall_context=history_recall_context,
            )
            messages = self._build_messages(
                query=state["query"],
                system_context=assembled.get("system_context", ""),
                recent_history=assembled.get("recent_history", []),
            )
            return {
                "system_context": assembled.get("system_context", ""),
                "recent_history": assembled.get("recent_history", []),
                "messages": messages,
                "node_timings": {"assemble_context": time.perf_counter() - started},
            }
        except SessionPersistenceError:
            raise
        except Exception as exc:
            logger.warning(
                "[agent graph] assemble_context degraded for session_id=%s, request_id=%s: %s",
                state["session_id"],
                state["request_id"],
                exc,
            )
            return {
                "system_context": "",
                "recent_history": [],
                "messages": [HumanMessage(content=state["query"])],
                "node_errors": [f"assemble_context: {exc}"],
                "node_timings": {"assemble_context": time.perf_counter() - started},
            }

    def _route_specialists(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if self.specialist_runner is None:
                return {
                    "specialist_routes": [
                        {
                            "agent_name": "LegacyAgent",
                            "reason": "legacy_single_agent_fallback",
                            "confidence": 1.0,
                        }
                    ],
                    "route_confidence": 1.0,
                    "node_timings": {"route_specialists": time.perf_counter() - started},
                }

            routed = self.specialist_router.route(
                query=state["query"],
                task_route=state.get("task_route") or {},
                history_recall_context=state.get("history_recall_context", ""),
                system_context=state.get("system_context", ""),
            )
            return {
                "specialist_routes": routed.get("specialist_routes", []),
                "route_confidence": float(routed.get("route_confidence", 0.0) or 0.0),
                "conflicts": routed.get("conflicts", []),
                "node_timings": {"route_specialists": time.perf_counter() - started},
            }
        except Exception as exc:
            logger.warning(
                "[agent graph] route_specialists degraded for session_id=%s, request_id=%s: %s",
                state["session_id"],
                state["request_id"],
                exc,
            )
            return {
                "specialist_routes": [],
                "route_confidence": 0.0,
                "conflicts": [f"router_error:{exc}"],
                "node_errors": [f"route_specialists: {exc}"],
                "node_timings": {"route_specialists": time.perf_counter() - started},
            }

    def _dispatch_specialists(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if self.specialist_runner is None:
                legacy_answer = self._invoke_legacy_agent(state)
                results = [
                    {
                        "agent_name": "LegacyAgent",
                        "summary": legacy_answer,
                        "confidence": 1.0,
                        "evidence": [],
                        "covered_points": [],
                        "unresolved_points": [],
                        "refusal_reason": None,
                    }
                ]
            else:
                results = self.specialist_runner.run(
                    routes=state.get("specialist_routes", []),
                    query=state["query"],
                    session_id=state["session_id"],
                    user_uuid=state["user_uuid"],
                    request_id=state["request_id"],
                    task_route=state.get("task_route") or {},
                    history_recall_context=state.get("history_recall_context", ""),
                    system_context=state.get("system_context", ""),
                    recent_history=state.get("recent_history", []),
                )
            return {
                "specialist_results": results,
                "node_timings": {"dispatch_specialists": time.perf_counter() - started},
            }
        except SessionPersistenceError:
            raise
        except Exception as exc:
            logger.warning(
                "[agent graph] dispatch_specialists failed for session_id=%s, request_id=%s: %s",
                state["session_id"],
                state["request_id"],
                exc,
            )
            return {
                "specialist_results": [
                    {
                        "agent_name": "dispatch_error",
                        "summary": "",
                        "confidence": 0.0,
                        "evidence": [],
                        "covered_points": [],
                        "unresolved_points": [str(exc)],
                        "refusal_reason": str(exc),
                    }
                ],
                "node_errors": [f"dispatch_specialists: {exc}"],
                "node_timings": {"dispatch_specialists": time.perf_counter() - started},
            }

    def _rewrite_query(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        raw_query = state["query"]
        task = (state.get("task_route") or {}).get("task")
        recent_history = state.get("recent_history", [])

        try:
            rewritten = self.query_rewriter.rewrite(raw_query, task, recent_history)
        except Exception as exc:
            logger.warning(
                "[agent graph] rewrite_query failed for session_id=%s, request_id=%s: %s",
                state["session_id"],
                state["request_id"],
                exc,
            )
            rewritten = raw_query

        return {
            "rewritten_query": rewritten,
            "node_timings": {"rewrite_query": time.perf_counter() - started},
        }

    def _agentic_answer(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        model_with_tools = chat_model.bind_tools(AGENTIC_TOOLS)

        user_query = state.get("rewritten_query") or state["query"]
        messages = self._build_messages(
            query=user_query,
            system_context=state.get("system_context", ""),
            recent_history=state.get("recent_history", []),
        )
        messages[-1] = HumanMessage(
            content=(
                f"当前用户问题: {user_query}\n\n"
                "请根据上下文和工具返回的信息，给出完整、准确的中文回答。"
                "如果需要查询知识库或外部数据，请调用对应工具。"
                "如果工具返回的信息不足或矛盾，请如实告知用户并给出建议。"
            )
        )

        tool_call_count = 0
        try:
            for _iteration in range(5):
                response = model_with_tools.invoke(messages)

                if not getattr(response, "tool_calls", None):
                    return {
                        "answer": self._message_content_to_text(response.content),
                        "tool_call_count": tool_call_count,
                        "node_timings": {"agentic_answer": time.perf_counter() - started},
                    }

                tool_call_count += len(response.tool_calls)
                messages.append(response)
                for tool_call in response.tool_calls:
                    tool_result = self._execute_single_tool(tool_call)
                    messages.append(
                        ToolMessage(
                            content=tool_result,
                            tool_call_id=tool_call["id"],
                        )
                    )

            messages.append(
                HumanMessage(
                    content="请基于以上所有工具返回的信息，给出最终中文回答。"
                    "如果有矛盾或信息不足，请明确指出。"
                )
            )
            final = model_with_tools.invoke(messages)
            return {
                "answer": self._message_content_to_text(final.content),
                "tool_call_count": tool_call_count,
                "node_timings": {"agentic_answer": time.perf_counter() - started},
            }
        except SessionPersistenceError:
            raise
        except Exception as exc:
            logger.warning(
                "[agent graph] agentic_answer failed for session_id=%s, request_id=%s: %s",
                state["session_id"],
                state["request_id"],
                exc,
            )
            return {
                "answer": "",
                "tool_call_count": tool_call_count,
                "node_errors": [f"agentic_answer: {exc}"],
                "node_timings": {"agentic_answer": time.perf_counter() - started},
            }

    def _execute_single_tool(self, tool_call: dict[str, Any]) -> str:
        name = str(tool_call.get("name", ""))
        args: dict[str, Any] = tool_call.get("args", {}) or {}
        fn = self._tool_map.get(name)
        if fn is None:
            return f"工具 '{name}' 不存在，可用工具: {list(self._tool_map.keys())}"
        try:
            result = fn.invoke(args) if hasattr(fn, "invoke") else fn(**args)
            return str(result) if result is not None else ""
        except Exception as exc:
            return f"工具调用失败 ({name}): {exc}"

    def _compose_answer(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        specialist_results = state.get("specialist_results", [])
        if not specialist_results:
            raise SessionPersistenceError("未能生成可用的专家结果，请稍后重试。")

        if self.specialist_runner is None:
            answer = str(specialist_results[0].get("summary", "")).strip()
            if not answer:
                raise SessionPersistenceError("未能生成可用的回答，请稍后重试。")
            return {
                "answer": answer,
                "composition_result": {
                    "mode": "legacy",
                    "conflicts": [],
                },
                "node_timings": {"compose_answer": time.perf_counter() - started},
            }

        usable_results = [
            result
            for result in specialist_results
            if str(result.get("summary", "")).strip()
        ]
        if not usable_results:
            raise SessionPersistenceError("所有专家都未能生成可用结果，请稍后重试。")

        answer, conflicts = self.answer_composer.compose(
            query=state["query"],
            task_route=state.get("task_route") or {},
            system_context=state.get("system_context", ""),
            history_recall_context=state.get("history_recall_context", ""),
            specialist_results=usable_results,
            recent_history=state.get("recent_history", []),
        )
        return {
            "answer": answer,
            "composition_result": {
                "mode": "multi_agent",
                "conflicts": conflicts,
            },
            "conflicts": conflicts,
            "node_timings": {"compose_answer": time.perf_counter() - started},
        }

    def _persist_turn(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        answer = state.get("answer", "").strip()
        if not answer:
            raise SessionPersistenceError("未能生成可用的回答，请稍后重试。")

        persisted = self.session_repository.persist_turn(
            session_id=state["session_id"],
            user_uuid=state["user_uuid"],
            request_id=state["request_id"],
            user_message=state["query"],
            assistant_message=answer,
        )
        return {
            "user_message_id": persisted["user_message_id"],
            "assistant_message_id": persisted["assistant_message_id"],
            "node_timings": {"persist_turn": time.perf_counter() - started},
        }

    def _update_task_state(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self.task_service.update_after_turn(
                route=state.get("task_route") or {"action": "no_task", "task": None},
                query=state["query"],
                assistant_message=state.get("answer", ""),
                session_id=state["session_id"],
                user_uuid=state["user_uuid"],
                request_id=state["request_id"],
                user_message_id=state["user_message_id"],
            )
        except Exception as exc:
            logger.warning(
                "[agent graph] update_task_state failed for session_id=%s, request_id=%s: %s",
                state["session_id"],
                state["request_id"],
                exc,
            )
            return {
                "node_errors": [f"update_task_state: {exc}"],
                "node_timings": {"update_task_state": time.perf_counter() - started},
            }
        return {"node_timings": {"update_task_state": time.perf_counter() - started}}

    def _refresh_user_memory(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self.memory_service.refresh_memories(
                session_id=state["session_id"],
                user_uuid=state["user_uuid"],
            )
        except Exception as exc:
            logger.warning(
                "[agent graph] refresh_user_memory failed for session_id=%s, request_id=%s: %s",
                state["session_id"],
                state["request_id"],
                exc,
            )
            return {
                "node_errors": [f"refresh_user_memory: {exc}"],
                "node_timings": {"refresh_user_memory": time.perf_counter() - started},
            }
        return {"node_timings": {"refresh_user_memory": time.perf_counter() - started}}

    def _invoke_legacy_agent(self, state: AgentGraphState) -> str:
        if self.inner_agent is None:
            raise SessionPersistenceError("未配置 legacy agent。")

        response = self.inner_agent.invoke(
            {"messages": state.get("messages", [])},
            config={"recursion_limit": 25},
        )
        if isinstance(response, dict):
            messages = response.get("messages", [])
            answer = self._extract_final_answer(messages)
            if answer:
                return answer
        if isinstance(response, str):
            return response.strip()
        return self._extract_final_answer(state.get("messages", []))

    @staticmethod
    def _build_messages(
        *,
        query: str,
        system_context: str,
        recent_history: list[dict[str, Any]],
    ) -> list[BaseMessage]:
        messages: list[BaseMessage] = []
        if system_context.strip():
            messages.append(SystemMessage(content=system_context.strip()))
        messages.extend(AgentGraphWorkflow._dict_history_to_messages(recent_history))
        messages.append(HumanMessage(content=query))
        return messages

    @staticmethod
    def _dict_history_to_messages(
        history: list[dict[str, Any]],
    ) -> list[BaseMessage]:
        messages: list[BaseMessage] = []
        for message in history:
            role = str(message.get("role", "user"))
            content = str(message.get("content", ""))
            if not content.strip():
                continue
            if role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))
            else:
                messages.append(HumanMessage(content=content))
        return messages

    @staticmethod
    def _extract_final_answer(messages: list[BaseMessage]) -> str:
        for message in reversed(messages):
            if isinstance(message, (AIMessage, AIMessageChunk)):
                tool_calls = getattr(message, "tool_calls", None) or []
                if tool_calls:
                    continue
                text = AgentGraphWorkflow._message_content_to_text(message.content)
                if text.strip():
                    return text.strip()
        return ""

    @staticmethod
    def _message_content_to_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            pieces: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    pieces.append(str(item.get("text", "")))
                else:
                    pieces.append(str(item))
            return "".join(pieces)
        return str(content)
