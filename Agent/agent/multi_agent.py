from __future__ import annotations

import concurrent.futures
import json
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from model.factory import chat_model
from rag.rag_service import RagSummarizeService
from utils.logger_handler import logger

try:
    from agent.tools.agent_tools import fetch_external_data, get_current_month, get_weather, get_user_location, user_ids
except Exception:  # pragma: no cover - fallback for partial imports in tests
    fetch_external_data = None
    get_current_month = None
    get_weather = None
    get_user_location = None
    user_ids = ["1001", "1002", "1003", "1004", "1005", "1006", "1007", "1008", "1009", "1010"]


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


FURNITURE_KEYWORDS = [
    "沙发",
    "床",
    "餐桌",
    "椅子",
    "衣柜",
    "灯具",
    "地毯",
    "书架",
    "书桌",
    "电脑桌",
    "茶几",
    "电视柜",
    "鞋柜",
    "玄关柜",
    "餐边柜",
    "床头柜",
    "儿童床",
    "储物柜",
    "斗柜",
    "办公椅",
    "吧台椅",
    "置物架",
    "阳台柜",
    "梳妆台",
    "橱柜",
    "浴室柜",
    "家具",
    "岩板",
    "修复",
]

DEVICE_KEYWORDS = [
    "扫地机器人",
    "扫拖一体机器人",
    "扫拖机",
    "拖布",
    "滚刷",
    "边刷",
    "尘盒",
    "基站",
    "雷达",
    "回充",
]

DEVICE_ISSUE_KEYWORDS = [
    "故障",
    "报错",
    "卡住",
    "异响",
    "清洁效率",
]

REPORT_KEYWORDS = [
    "报表",
    "报告",
    "数据",
    "使用记录",
    "统计",
    "分析",
    "本月",
    "这个月",
    "上个月",
    "耗材",
    "效率",
    "对比",
    "特征",
]

REPORT_ANCHOR_KEYWORDS = [
    "报表",
    "报告",
    "数据",
    "使用记录",
    "统计",
    "本月",
    "这个月",
    "上个月",
    "耗材",
    "对比",
    "查询",
    "查看",
]

REPORT_SOFT_KEYWORDS = [
    "效率",
    "分析",
    "特征",
]

NON_BUSINESS_SENSITIVE_KEYWORDS = [
    "系统提示词",
    "数据库密码",
    "SQL",
    "users 表",
    "全部会话",
    "数据库连接字符串",
    "API_KEY",
    "JWT",
    "内部工具",
    "ToolMessage",
    "思维过程",
    "user_uuid",
    "Chroma collection",
    "环境变量",
]

GENERAL_KEYWORDS = [
    "你好",
    "在吗",
    "天气",
    "能不能",
    "帮我澄清",
    "什么意思",
    "怎么理解",
    "刚才那个",
    "继续",
    "追问",
]

CITY_CANDIDATES = [
    "北京",
    "上海",
    "深圳",
    "广州",
    "杭州",
    "合肥",
    "成都",
    "武汉",
    "南京",
    "苏州",
    "天津",
    "重庆",
    "长沙",
    "郑州",
    "西安",
    "宁波",
    "厦门",
    "青岛",
    "佛山",
    "东莞",
]

KNOWLEDGE_AGENT = "KnowledgeAgent"
REPORT_AGENT = "ReportAgent"
DEFAULT_RESPONDER = "DefaultResponder"

DOMAIN_FURNITURE = "furniture"
DOMAIN_ROBOT_VACUUM = "robot_vacuum"
DOMAIN_MIXED = "mixed"


@dataclass
class SpecialistResult:
    agent_name: str
    summary: str
    confidence: float
    evidence: list[str]
    covered_points: list[str]
    unresolved_points: list[str]
    status: str = "answered"
    refusal_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "summary": self.summary,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "covered_points": self.covered_points,
            "unresolved_points": self.unresolved_points,
            "status": self.status,
            "refusal_reason": self.refusal_reason,
        }


class MultiAgentRouter:
    def __init__(self):
        self._fallback_model = chat_model

    def route(
        self,
        *,
        query: str,
        task_route: dict[str, Any] | None = None,
        history_recall_context: str = "",
        system_context: str = "",
    ) -> dict[str, Any]:
        rules = self._route_by_rules(
            query=query,
            task_route=task_route or {},
            history_recall_context=history_recall_context,
            system_context=system_context,
        )
        if rules.get("default_response"):
            return rules
        if rules["specialist_routes"] and rules["route_confidence"] >= 0.6:
            return rules

        fallback = self._route_with_model(
            query=query,
            task_route=task_route or {},
            history_recall_context=history_recall_context,
            system_context=system_context,
            fallback=rules,
        )
        return fallback or rules

    def _route_by_rules(
        self,
        *,
        query: str,
        task_route: dict[str, Any],
        history_recall_context: str,
        system_context: str,
    ) -> dict[str, Any]:
        task = task_route.get("task") or {}
        query_furniture_score = self._score(query, FURNITURE_KEYWORDS)
        query_device_score = self._device_score(query, query_furniture_score)
        query_report_score = self._report_score(query)
        query_general_score = self._score(query, GENERAL_KEYWORDS)
        is_referential = any(
            marker in query for marker in ["刚才", "那个", "继续", "上次", "之前", "它"]
        )
        context = (
            f"{task.get('topic', '')} {task.get('goal', '')} "
            f"{history_recall_context} {system_context}"
            if is_referential
            else ""
        )
        normalized = f"{query} {context}"
        furniture_score = self._score(normalized, FURNITURE_KEYWORDS)
        device_score = self._device_score(normalized, furniture_score)
        report_score = self._report_score(normalized)
        general_score = self._score(normalized, GENERAL_KEYWORDS)

        if self._is_non_business_sensitive(query) and max(furniture_score, device_score) == 0:
            return {
                "specialist_routes": [],
                "route_confidence": 0.98,
                "conflicts": [],
                "default_response": True,
                "default_reason": "non_business_sensitive_query",
            }

        if report_score > 0 and self._score(normalized, DEVICE_KEYWORDS) == 0:
            device_score = 0

        routes: list[dict[str, Any]] = []

        if query_general_score > 0 and max(
            query_furniture_score,
            query_device_score,
            query_report_score,
        ) == 0 and not is_referential:
            return {
                "specialist_routes": [],
                "route_confidence": 0.96,
                "conflicts": [],
                "default_response": True,
                "default_reason": "general_or_clarification_query",
            }

        if report_score > 0:
            routes.append(
                {
                    "agent_name": REPORT_AGENT,
                    "reason": "report_query",
                    "confidence": min(0.96, 0.72 + report_score * 0.06),
                }
            )

        if furniture_score > 0 or device_score > 0:
            if furniture_score > 0 and device_score > 0:
                domain = DOMAIN_MIXED
            elif device_score > 0:
                domain = DOMAIN_ROBOT_VACUUM
            else:
                domain = DOMAIN_FURNITURE
            knowledge_score = max(furniture_score, device_score)
            routes.append(
                {
                    "agent_name": KNOWLEDGE_AGENT,
                    "domain": domain,
                    "reason": f"knowledge_query:{domain}",
                    "confidence": min(0.96, 0.70 + knowledge_score * 0.05),
                }
            )

        routes = self._dedupe_and_limit(routes)

        if not routes:
            return {
                "specialist_routes": [],
                "route_confidence": 0.55,
                "conflicts": [],
                "default_response": True,
                "default_reason": "fallback_default",
            }

        route_confidence = max(route["confidence"] for route in routes)
        return {
            "specialist_routes": routes,
            "route_confidence": route_confidence,
            "conflicts": [],
        }

    def _route_with_model(
        self,
        *,
        query: str,
        task_route: dict[str, Any],
        history_recall_context: str,
        system_context: str,
        fallback: dict[str, Any],
    ) -> dict[str, Any] | None:
        prompt = f"""
You are a router for a multi-agent customer service system.
Choose up to two specialists from: KnowledgeAgent, ReportAgent.
KnowledgeAgent handles furniture and robot-vacuum knowledge. It must include domain:
- furniture
- robot_vacuum
- mixed
ReportAgent handles reports, usage records, cleaning efficiency, statistics, and device usage data.
Greeting, weather, clarification, or non-business questions are handled by DefaultResponder, which is not a specialist. For those cases return an empty specialist_routes list.
Return JSON only in this format:
{{
  "specialist_routes": [
    {{"agent_name": "KnowledgeAgent", "domain": "furniture", "reason": "...", "confidence": 0.0}}
  ],
  "route_confidence": 0.0,
  "conflicts": []
}}

Query:
{query}

Task route:
{json.dumps(task_route, ensure_ascii=False, default=str)}

History recall:
{history_recall_context}

System context:
{system_context}
"""
        try:
            response = self._fallback_model.invoke(
                [SystemMessage(content=prompt.strip()), HumanMessage(content=query)]
            )
            content = _message_content_to_text(response.content)
            parsed = json.loads(self._strip_json(content))
            if not isinstance(parsed, dict):
                return None
            routes = parsed.get("specialist_routes")
            if not isinstance(routes, list):
                return None
            parsed["specialist_routes"] = self._dedupe_and_limit(
                [
                    {
                        "agent_name": str(route.get("agent_name", "")).strip(),
                        "domain": str(route.get("domain", "")).strip(),
                        "reason": str(route.get("reason", "")).strip(),
                        "confidence": float(route.get("confidence", 0.0) or 0.0),
                    }
                    for route in routes
                    if str(route.get("agent_name", "")).strip()
                ]
            )
            parsed["route_confidence"] = float(parsed.get("route_confidence", 0.0) or 0.0)
            parsed.setdefault("conflicts", [])
            if not parsed["specialist_routes"]:
                parsed["default_response"] = True
                parsed.setdefault("default_reason", "model_default_response")
                parsed["route_confidence"] = max(parsed["route_confidence"], 0.55)
            return parsed
        except Exception as exc:
            logger.info("[router] model fallback failed, using rules: %s", exc)
            return fallback

    @classmethod
    def _dedupe_and_limit(cls, routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for route in routes:
            normalized = cls._normalize_route(route)
            if not normalized:
                continue

            name = normalized["agent_name"]
            if name == KNOWLEDGE_AGENT and name in seen:
                existing = next(item for item in deduped if item["agent_name"] == KNOWLEDGE_AGENT)
                existing["domain"] = cls._merge_domains(existing.get("domain", ""), normalized.get("domain", ""))
                existing["confidence"] = max(
                    float(existing.get("confidence", 0.0) or 0.0),
                    float(normalized.get("confidence", 0.0) or 0.0),
                )
                continue

            if name in seen:
                continue
            seen.add(name)
            deduped.append(normalized)
            if len(deduped) >= 2:
                break
        return deduped

    @classmethod
    def _normalize_route(cls, route: dict[str, Any]) -> dict[str, Any] | None:
        name = str(route.get("agent_name", "")).strip()
        domain = str(route.get("domain", "")).strip()

        if name == "FurnitureAgent":
            name = KNOWLEDGE_AGENT
            domain = DOMAIN_FURNITURE
        elif name == "DeviceAgent":
            name = KNOWLEDGE_AGENT
            domain = DOMAIN_ROBOT_VACUUM
        elif name in {"GeneralAgent", DEFAULT_RESPONDER}:
            return None

        if name not in {KNOWLEDGE_AGENT, REPORT_AGENT}:
            return None

        normalized = {
            "agent_name": name,
            "reason": str(route.get("reason", "")).strip(),
            "confidence": float(route.get("confidence", 0.0) or 0.0),
        }
        if name == KNOWLEDGE_AGENT:
            normalized["domain"] = cls._normalize_domain(domain)
        return normalized

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        if domain in {DOMAIN_FURNITURE, DOMAIN_ROBOT_VACUUM, DOMAIN_MIXED}:
            return domain
        return DOMAIN_MIXED

    @staticmethod
    def _merge_domains(left: str, right: str) -> str:
        left = MultiAgentRouter._normalize_domain(left)
        right = MultiAgentRouter._normalize_domain(right)
        if left == right:
            return left
        return DOMAIN_MIXED

    @staticmethod
    def _score(text: str, keywords: list[str]) -> int:
        return sum(1 for keyword in keywords if keyword and keyword in text)

    @classmethod
    def _report_score(cls, text: str) -> int:
        anchor_score = cls._score(text, REPORT_ANCHOR_KEYWORDS)
        soft_score = cls._score(text, REPORT_SOFT_KEYWORDS)
        if anchor_score == 0:
            return 0
        return anchor_score + soft_score

    @classmethod
    def _is_non_business_sensitive(cls, text: str) -> bool:
        return cls._score(text, NON_BUSINESS_SENSITIVE_KEYWORDS) > 0

    @classmethod
    def _device_score(cls, text: str, furniture_score: int) -> int:
        object_score = cls._score(text, DEVICE_KEYWORDS)
        if object_score:
            return object_score + cls._score(text, DEVICE_ISSUE_KEYWORDS)
        if furniture_score == 0:
            return cls._score(text, DEVICE_ISSUE_KEYWORDS)
        return 0

    @staticmethod
    def _message_content(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        return str(content)

    @staticmethod
    def _strip_json(text: str) -> str:
        match = re.search(r"\{.*\}", text, flags=re.S)
        return match.group(0) if match else text


class MultiAgentRunner:
    def __init__(self, *, rag_service: RagSummarizeService | None = None):
        self.rag_service = rag_service or RagSummarizeService()

    def run(
        self,
        *,
        routes: list[dict[str, Any]],
        query: str,
        session_id: str,
        user_uuid: str,
        request_id: str,
        task_route: dict[str, Any] | None = None,
        history_recall_context: str = "",
        system_context: str = "",
        recent_history: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not routes:
            return [
                self._run_default_responder(
                    query=query,
                    task_route=task_route or {},
                    history_recall_context=history_recall_context,
                    system_context=system_context,
                    recent_history=recent_history or [],
                )
            ]

        results: list[tuple[int, dict[str, Any]]] = []
        max_workers = min(2, len(routes))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    self._run_specialist,
                    route=route,
                    query=query,
                    session_id=session_id,
                    user_uuid=user_uuid,
                    request_id=request_id,
                    task_route=task_route or {},
                    history_recall_context=history_recall_context,
                    system_context=system_context,
                    recent_history=recent_history or [],
                ): index
                for index, route in enumerate(routes)
            }
            for future in concurrent.futures.as_completed(future_map):
                index = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    route = routes[index]
                    logger.warning(
                        "[multi agent] specialist %s failed for request_id=%s: %s",
                        route.get("agent_name"),
                        request_id,
                        exc,
                        exc_info=True,
                    )
                    result = SpecialistResult(
                        agent_name=str(route.get("agent_name", "unknown")),
                        summary="",
                        confidence=0.0,
                        evidence=[],
                        covered_points=[],
                        unresolved_points=[],
                        status="refused",
                        refusal_reason=str(exc),
                    ).as_dict()
                results.append((index, result))

        return [item[1] for item in sorted(results, key=lambda pair: pair[0])]

    def _run_specialist(
        self,
        *,
        route: dict[str, Any],
        query: str,
        session_id: str,
        user_uuid: str,
        request_id: str,
        task_route: dict[str, Any],
        history_recall_context: str,
        system_context: str,
        recent_history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        agent_name = route.get("agent_name", "")
        if agent_name == KNOWLEDGE_AGENT:
            domain = MultiAgentRouter._normalize_domain(str(route.get("domain", "")))
            return self._run_rag_specialist(
                agent_name=KNOWLEDGE_AGENT,
                domain=domain,
                query=query,
                route=route,
            )
        if agent_name == "FurnitureAgent":
            return self._run_rag_specialist(
                agent_name=KNOWLEDGE_AGENT,
                domain=DOMAIN_FURNITURE,
                query=query,
                route=route,
            )
        if agent_name == "DeviceAgent":
            return self._run_rag_specialist(
                agent_name=KNOWLEDGE_AGENT,
                domain=DOMAIN_ROBOT_VACUUM,
                query=query,
                route=route,
            )
        if agent_name == REPORT_AGENT:
            return self._run_report_specialist(query=query, user_uuid=user_uuid)
        return self._run_default_responder(
            query=query,
            task_route=task_route,
            history_recall_context=history_recall_context,
            system_context=system_context,
            recent_history=recent_history,
        )

    def _run_rag_specialist(
        self,
        *,
        agent_name: str,
        domain: str,
        query: str,
        route: dict[str, Any],
    ) -> dict[str, Any]:
        allowed_domains = (
            [DOMAIN_FURNITURE, DOMAIN_ROBOT_VACUUM]
            if domain == DOMAIN_MIXED
            else [domain]
        )
        retrieval = self.rag_service.retrieve(query, allowed_domains=allowed_domains)
        summary = self.rag_service.answer(query, retrieval)
        status = "answered" if retrieval.evidence_sufficient else "refused"
        evidence = [
            self._render_candidate(candidate)
            for candidate in retrieval.selected[:3]
        ]
        covered_points = self._collect_covered_points(retrieval.selected)
        unresolved_points = [retrieval.refusal_reason] if retrieval.refusal_reason else []
        result = SpecialistResult(
            agent_name=agent_name,
            summary=summary,
            confidence=float(retrieval.selected[0].rerank_score or 0.0) if retrieval.selected else 0.0,
            evidence=evidence,
            covered_points=covered_points,
            unresolved_points=unresolved_points,
            status=status,
            refusal_reason=retrieval.refusal_reason,
        ).as_dict()
        result["domain"] = domain
        result["allowed_domains"] = allowed_domains
        return result

    def _run_report_specialist(self, *, query: str, user_uuid: str) -> dict[str, Any]:
        if fetch_external_data is None or get_current_month is None:
            return SpecialistResult(
                agent_name="ReportAgent",
                summary="",
                confidence=0.0,
                evidence=[],
                covered_points=[],
                unresolved_points=["report_tools_unavailable"],
                status="refused",
                refusal_reason="report_tools_unavailable",
            ).as_dict()

        user_id = self._derive_external_user_id(user_uuid)
        month = self._extract_month(query) or get_current_month()
        report = fetch_external_data(user_id, month)
        if not report:
            summary = f"未查到用户 {user_id} 在 {month} 的使用记录。"
            return SpecialistResult(
                agent_name="ReportAgent",
                summary=summary,
                confidence=0.35,
                evidence=[],
                covered_points=[],
                unresolved_points=["no_external_usage_record"],
                refusal_reason="no_external_usage_record",
            ).as_dict()

        summary = f"用户 {user_id} 在 {month} 的使用记录显示：{report}。"
        return SpecialistResult(
            agent_name="ReportAgent",
            summary=summary,
            confidence=0.8,
            evidence=[report],
            covered_points=["usage_report"],
            unresolved_points=[],
            refusal_reason=None,
        ).as_dict()

    def _run_default_responder(
        self,
        *,
        query: str,
        task_route: dict[str, Any],
        history_recall_context: str,
        system_context: str,
        recent_history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self._looks_like_weather(query):
            city = self._extract_city(query) or self._default_city()
            if get_weather is None:
                summary = f"当前无法查询天气，但你可以告诉我城市名，我再帮你查。"
                return SpecialistResult(
                    agent_name=DEFAULT_RESPONDER,
                    summary=summary,
                    confidence=0.4,
                    evidence=[],
                    covered_points=[],
                    unresolved_points=["weather_tool_unavailable"],
                    refusal_reason="weather_tool_unavailable",
                ).as_dict()
            weather = get_weather(city)
            return SpecialistResult(
                agent_name=DEFAULT_RESPONDER,
                summary=weather,
                confidence=0.82,
                evidence=[weather],
                covered_points=["weather"],
                unresolved_points=[],
                refusal_reason=None,
            ).as_dict()

        if self._is_pure_greeting(query):
            task_route = {}
            history_recall_context = ""
            system_context = ""
            recent_history = []

        prompt = f"""
You are DefaultResponder in a customer service system.
Answer briefly, helpfully, and ask one clear clarification question if the request is ambiguous.
Use the provided context, but do not invent facts.

Current task:
{json.dumps(task_route, ensure_ascii=False, default=str)}

Historical recall:
{history_recall_context}

System context:
{system_context}

Recent history:
{json.dumps(recent_history, ensure_ascii=False, default=str)}
"""
        response = chat_model.invoke(
            [
                SystemMessage(content=prompt.strip()),
                HumanMessage(content=query),
            ]
        )
        summary = _message_content_to_text(response.content).strip()
        return SpecialistResult(
            agent_name=DEFAULT_RESPONDER,
            summary=summary,
            confidence=0.58 if summary else 0.0,
            evidence=[],
            covered_points=[],
            unresolved_points=[] if summary else ["empty_general_response"],
            status="answered" if summary else "refused",
            refusal_reason=None if summary else "empty_general_response",
        ).as_dict()

    def _run_general_specialist(
        self,
        *,
        query: str,
        task_route: dict[str, Any],
        history_recall_context: str,
        system_context: str,
        recent_history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._run_default_responder(
            query=query,
            task_route=task_route,
            history_recall_context=history_recall_context,
            system_context=system_context,
            recent_history=recent_history,
        )

    @staticmethod
    def _is_pure_greeting(query: str) -> bool:
        normalized = re.sub(r"[\s，。！？,.!?]", "", query)
        return normalized in {"你好", "您好", "在吗", "你好在吗", "嗨", "哈喽"}

    @staticmethod
    def _derive_external_user_id(user_uuid: str) -> str:
        if not user_ids:
            return "1001"
        total = sum(ord(char) for char in user_uuid)
        return user_ids[total % len(user_ids)]

    @staticmethod
    def _extract_month(query: str) -> str | None:
        match = re.search(r"(20\d{2}-\d{2})", query)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_city(query: str) -> str | None:
        for city in CITY_CANDIDATES:
            if city in query:
                return city
        return None

    @staticmethod
    def _default_city() -> str:
        if get_user_location is None:
            return "深圳"
        try:
            return get_user_location()
        except Exception:
            return "深圳"

    @staticmethod
    def _looks_like_weather(query: str) -> bool:
        return "天气" in query or "气温" in query or "下雨" in query

    @staticmethod
    def _render_candidate(candidate: Any) -> str:
        source_name = candidate.metadata.get("source_name", "unknown")
        category = candidate.metadata.get("category", "general")
        intent = candidate.metadata.get("intent", "general")
        return f"{source_name} | {category} | {intent}: {candidate.content[:180]}"

    @staticmethod
    def _collect_covered_points(candidates: list[Any]) -> list[str]:
        covered: list[str] = []
        for candidate in candidates:
            source_name = str(candidate.metadata.get("source_name", ""))
            category = str(candidate.metadata.get("category", ""))
            intent = str(candidate.metadata.get("intent", ""))
            point = " / ".join(part for part in [source_name, category, intent] if part)
            if point and point not in covered:
                covered.append(point)
        return covered[:5]


class AnswerComposer:
    def __init__(self):
        self.model = chat_model

    def compose(
        self,
        *,
        query: str,
        task_route: dict[str, Any],
        system_context: str,
        history_recall_context: str,
        specialist_results: list[dict[str, Any]],
        recent_history: list[dict[str, Any]],
    ) -> tuple[str, list[str]]:
        conflicts = self._collect_conflicts(specialist_results)
        if len(specialist_results) == 1:
            direct_answer = str(specialist_results[0].get("summary", "")).strip()
            if direct_answer:
                return direct_answer, conflicts

        answered_results = [
            result
            for result in specialist_results
            if result.get("status", "answered") == "answered"
            and str(result.get("summary", "")).strip()
        ]
        if not answered_results:
            refusal = next(
                (
                    str(result.get("summary", "")).strip()
                    for result in specialist_results
                    if str(result.get("summary", "")).strip()
                ),
                "",
            )
            if refusal:
                return refusal, conflicts

        prompt = f"""
You are AnswerComposer.
Write the final user-facing answer in Chinese.
Use only the provided information.
Priority:
1. Current user input
2. MySQL facts / task state
3. Structured external data
4. RAG evidence
5. Limited wording inference, never new factual claims
If KnowledgeAgent refused because RAG evidence is insufficient, keep that refusal for the knowledge part and do not fill it with common sense.
If the information conflicts or is insufficient, answer the confirmed part only and ask one clear clarification question.
Keep the answer concise, accurate, and natural.

Current user input:
{query}

Task route:
{json.dumps(task_route, ensure_ascii=False, default=str)}

System context:
{system_context}

Historical recall:
{history_recall_context}

Recent history:
{json.dumps(recent_history, ensure_ascii=False, default=str)}

Specialist results:
{json.dumps(specialist_results, ensure_ascii=False, default=str)}

Conflicts:
{json.dumps(conflicts, ensure_ascii=False, default=str)}
"""
        response = self.model.invoke(
            [
                SystemMessage(content=prompt.strip()),
                HumanMessage(content=query),
            ]
        )
        answer = _message_content_to_text(response.content).strip()
        if not answer:
            raise RuntimeError("empty composition result")
        return answer, conflicts

    @staticmethod
    def _collect_conflicts(results: list[dict[str, Any]]) -> list[str]:
        conflicts: list[str] = []
        for result in results:
            for conflict in result.get("conflicts") or []:
                text = str(conflict).strip()
                if text:
                    conflicts.append(f"{result.get('agent_name', 'unknown')}: {text}")
        return conflicts[:10]

    @staticmethod
    def _message_content(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        return str(content)
