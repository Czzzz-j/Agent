from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from utils.logger_handler import logger

PURE_REFERENTIAL = {"它", "那个", "这个", "那", "这", "他", "她"}
REFERENTIAL_MARKERS = [
    "它",
    "那个",
    "这个",
    "之前那个",
    "上次那个",
    "上面提到的",
    "刚刚那个",
    "前面说的",
    "他",
    "她",
]


class QueryRewriter:
    def __init__(self, model=None):
        if model is not None:
            self._model = model
            self._model_initialized = True
        else:
            self._model = None
            self._model_initialized = False

    def _get_model(self):
        if self._model_initialized:
            return self._model
        self._model_initialized = True
        try:
            from model.factory import memory_model

            self._model = memory_model
        except Exception as exc:
            logger.warning("[query rewriter] model init failed: %s", exc)
            self._model = None
        return self._model

    def rewrite(
        self,
        query: str,
        task: dict[str, Any] | None,
        recent_history: list[dict[str, Any]] | None = None,
    ) -> str:
        cleaned = (query or "").strip()
        if not cleaned:
            return cleaned

        # 第1层：纯指代词 → task.topic 直接替换
        rule_result = self._rule_rewrite(cleaned, task)
        if rule_result is not None:
            return rule_result

        # 第2层：不需要改写的直接返回
        if not self._needs_rewrite(cleaned):
            return cleaned

        # 第3层：LLM 改写
        return self._llm_rewrite(cleaned, task, recent_history or [])

    def _rule_rewrite(self, query: str, task: dict[str, Any] | None) -> str | None:
        normalized = re.sub(r"[，。！？,.!?\s]", "", query)
        if normalized in PURE_REFERENTIAL:
            topic = (task or {}).get("topic", "").strip() if task else ""
            if topic:
                return topic
            return query
        return None

    def _needs_rewrite(self, query: str) -> bool:
        return any(marker in query for marker in REFERENTIAL_MARKERS)

    def _llm_rewrite(
        self,
        query: str,
        task: dict[str, Any] | None,
        recent_history: list[dict[str, Any]],
    ) -> str:
        model = self._get_model()
        if model is None:
            return self._fallback_rewrite(query, task)

        topic = (task or {}).get("topic", "") if task else ""
        goal = (task or {}).get("goal", "") if task else ""

        history_lines: list[str] = []
        for msg in recent_history[-6:]:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = str(msg.get("content", ""))[:200]
            if content.strip():
                history_lines.append(f"{role}: {content}")

        prompt = (
            "把用户的模糊指代转换为具体内容。使用对话上下文中的信息来确定指代对象。\n\n"
            f"当前任务主题: {topic or '无'}\n"
            f"当前任务目标: {goal or '无'}\n"
            f"最近对话:\n{chr(10).join(history_lines) if history_lines else '无'}\n\n"
            f"用户原话: {query}\n\n"
            "只输出改写后的完整查询，不输出任何解释、标点或额外内容。"
        )

        try:
            response = model.invoke([
                SystemMessage(content="你是查询改写器。只输出改写后的查询文本，不输出其他内容。"),
                HumanMessage(content=prompt),
            ])
            result = self._message_to_text(response.content).strip()
            cleaned_result = result.split("\n")[0].strip()
            if cleaned_result and len(cleaned_result) >= 2:
                return cleaned_result
            return self._fallback_rewrite(query, task)
        except Exception as exc:
            logger.info("[query rewriter] LLM rewrite failed, using fallback: %s", exc)
            return self._fallback_rewrite(query, task)

    @staticmethod
    def _fallback_rewrite(query: str, task: dict[str, Any] | None) -> str:
        topic = (task or {}).get("topic", "").strip() if task else ""
        if topic and topic not in query:
            return f"{query}（{topic}）"
        return query

    @staticmethod
    def _message_to_text(content: Any) -> str:
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
