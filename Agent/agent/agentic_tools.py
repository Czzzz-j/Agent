from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from rag.rag_service import RagSummarizeService, STRICT_REFUSAL

try:
    from agent.tools.agent_tools import fetch_external_data, get_user_location
except Exception:
    fetch_external_data = None  # type: ignore[assignment]
    get_user_location = None  # type: ignore[assignment]

_rag_service: RagSummarizeService | None = None


def _get_rag_service() -> RagSummarizeService:
    global _rag_service
    if _rag_service is None:
        _rag_service = RagSummarizeService()
    return _rag_service


@tool(description="Search the furniture/home-device knowledge base. "
      "Use this tool when you need factual evidence about furniture selection, cleaning, "
      "maintenance, troubleshooting, or smart device issues. "
      "Parameters: query (required) - the search query in Chinese; "
      "domain (optional) - 'furniture' for furniture questions, 'robot_vacuum' for device questions, "
      "omit or leave empty to search all domains. "
      "Returns the RAG-generated answer with evidence citations, or a refusal message "
      "if no sufficient evidence found.")
def search_knowledge_base(query: str, domain: str = "") -> str:
    allowed_domains: list[str] | None = None
    if domain and domain in {"furniture", "robot_vacuum"}:
        allowed_domains = [domain]
    return _get_rag_service().rag_summarize(query, allowed_domains=allowed_domains)


@tool(description="Query a user's device usage report for a specific month from external systems "
      "(MySQL + Redis cache). Use this tool when the user asks about their usage statistics, "
      "reports, efficiency data, consumable usage, or monthly summaries for their smart devices. "
      "Parameters: user_uuid (required) - the user's unique identifier; "
      "month (optional) - month in 'YYYY-MM' format, omit for current month. "
      "Returns the usage report string, or an empty string if no record found.")
def query_user_report(user_uuid: str, month: str = "") -> str:
    if fetch_external_data is None:
        return "报表查询服务暂不可用，请联系人工客服获取使用报告。"
    import random

    actual_month = month.strip() if month and month.strip() else f"2025-{random.choice(['01','02','03','04','05','06','07','08','09','10','11','12'])}"
    return fetch_external_data(user_id=user_uuid, month=actual_month)


@tool(description="Get the current weather for a given city. "
      "Use this tool when the user asks about weather conditions, temperature, humidity, "
      "or rain probability for a specific city. "
      "Parameters: city (required) - the city name in Chinese (e.g. '深圳', '北京'). "
      "Returns a weather summary string.")
def get_current_weather(city: str) -> str:
    try:
        from agent.tools.agent_tools import get_weather
    except Exception:
        get_weather = None

    if get_weather is None:
        return f"天气查询服务暂不可用，请稍后再试。"
    return get_weather(city)


def build_tool_map() -> dict[str, Any]:
    """返回 name -> callable 的映射，供 agentic_answer 分发用。"""
    return {
        "search_knowledge_base": search_knowledge_base,
        "query_user_report": query_user_report,
        "get_current_weather": get_current_weather,
    }


AGENTIC_TOOLS: list[Any] = [search_knowledge_base, query_user_report, get_current_weather]
