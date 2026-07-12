import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from db.memory_repository import MemoryRepository
from db.session_repository import SessionRepository
from utils.logger_handler import logger


FURNITURE_KEYWORDS = [
    "沙发", "床", "餐桌", "衣柜", "灯具", "地毯", "书柜", "鞋柜",
    "电视柜", "梳妆台", "茶几", "电脑桌", "浴室柜", "橱柜", "扫地机器人",
]
ENVIRONMENT_KEYWORDS = ["南方", "北方", "潮湿", "有猫", "有狗", "小户型", "木地板", "地砖", "老人", "孩子"]
PREFERENCE_KEYWORDS = ["喜欢", "偏好", "更喜欢", "希望", "最好", "优先", "倾向"]
AVOIDANCE_KEYWORDS = ["不想", "不要", "避免", "别用", "讨厌", "不喜欢", "不接受"]
ATTEMPT_KEYWORDS = ["试过", "已经", "用了", "重启", "清洗", "擦过", "更换", "联系过", "处理过"]
ISSUE_KEYWORDS = ["一直", "总是", "经常", "老是", "反复", "又", "异响", "故障", "卡住", "失灵", "划痕", "发霉"]
EXPLICIT_FACT_MARKERS = ["我家", "家里", "我们家", "买了", "用的是", "预算", "希望", "不要", "不想", "喜欢", "有猫", "有狗", "养了"]


class MemoryService:
    def __init__(
        self,
        session_repository: SessionRepository,
        memory_repository: MemoryRepository,
        max_turns: int = 10,
    ):
        self.session_repository = session_repository
        self.memory_repository = memory_repository
        self.short_term_limit = max_turns * 2
        self.summary_initial_threshold = 16
        self.summary_refresh_delta = 8

    def build_memory_context(self, session_id: str, user_uuid: str) -> str:
        sections: list[str] = []

        user_summary = self._build_user_memory_summary(user_uuid)
        if user_summary:
            sections.append("Long-term user memory:\n" + user_summary)

        session_summary = self.memory_repository.get_session_memory(session_id)
        if session_summary:
            sections.append("Session summary:\n" + self._format_session_summary(session_summary["summary"]))

        if not sections:
            return ""
        return "\n\n".join(sections)

    def refresh_memories(self, session_id: str, user_uuid: str) -> None:
        stats = self.session_repository.get_session_message_stats(session_id)
        if stats["message_count"] == 0:
            return

        messages = self.session_repository.get_session_messages(session_id, limit=40)
        if not messages:
            return

        try:
            self._refresh_session_summary(session_id, stats, messages)
        except Exception as exc:
            logger.warning("[memory] failed to refresh session summary for session_id=%s: %s", session_id, exc)

        try:
            self._refresh_user_memory(user_uuid, session_id, messages)
        except Exception as exc:
            logger.warning("[memory] failed to refresh user memory for user_uuid=%s: %s", user_uuid, exc)

    def refresh_long_term_memory(self, session_id: str, user_uuid: str) -> None:
        messages = self.session_repository.get_session_messages(session_id, limit=4)
        if not messages:
            return
        self._refresh_user_memory(user_uuid, session_id, messages)

    def _refresh_session_summary(
        self,
        session_id: str,
        stats: dict[str, int],
        messages: list[dict[str, Any]],
    ) -> None:
        if stats["message_count"] < self.summary_initial_threshold:
            return

        existing = self.memory_repository.get_session_memory(session_id)
        if existing:
            previous_count = self.session_repository.count_messages_up_to(
                session_id,
                int(existing["source_message_upto_id"]),
            )
            if stats["message_count"] - previous_count < self.summary_refresh_delta:
                return

        summary = self._build_session_summary(messages)
        self.memory_repository.upsert_session_memory(
            session_id=session_id,
            summary=summary,
            source_message_upto_id=stats["latest_message_id"],
        )

    def _refresh_user_memory(
        self,
        user_uuid: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        all_extracted: dict[str, list[str]] = {}
        latest_user_msg_id = 0
        for message in messages:
            if message["role"] != "user":
                continue
            per_message = self._extract_user_memory(message["content"])
            for memory_key, new_values in per_message.items():
                all_extracted.setdefault(memory_key, []).extend(new_values)
            latest_user_msg_id = max(latest_user_msg_id, int(message["id"]))

        if not all_extracted:
            return

        all_extracted = {
            key: self._dedupe_preserve_order(values)[:6]
            for key, values in all_extracted.items()
        }

        existing_map = self.memory_repository.get_user_memory_map(user_uuid)
        for memory_key, new_values in all_extracted.items():
            current_values = existing_map.get(memory_key, {}).get("memory_value", [])
            merged_values = self._merge_memory_values(memory_key, current_values, new_values)
            if merged_values == current_values:
                continue

            self.memory_repository.upsert_user_memory(
                user_uuid=user_uuid,
                memory_key=memory_key,
                memory_value=merged_values,
                confidence=0.90,
                source_session_id=session_id,
                source_message_id=latest_user_msg_id,
            )

    def _build_session_summary(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            llm_result = self._build_session_summary_llm(messages)
            if llm_result and llm_result.get("current_goal"):
                return llm_result
        except Exception as exc:
            logger.info("[memory] LLM session summary failed, falling back to rules: %s", exc)
        return self._build_session_summary_rules(messages)

    def _build_session_summary_llm(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        from model.factory import memory_model

        transcript = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {str(m['content'])[:300]}"
            for m in messages[-40:]
        )
        prompt = f"""Summarize this furniture customer service conversation. Return ONLY JSON:

{{
  "current_goal": "user's current main goal or request (one sentence, max 100 chars)",
  "objects_in_discussion": ["furniture/device items discussed"],
  "confirmed_facts": ["facts confirmed in this conversation"],
  "attempted_actions": ["actions user has tried"],
  "open_questions": ["unresolved user questions"],
  "constraints": ["budget, environment, preferences constraints"]
}}

Rules: Be concise. Only include info from the transcript. Return exactly the JSON.

Conversation:
{transcript}

JSON:"""
        response = memory_model.invoke([
            SystemMessage(content="You are a precise conversation summarizer. Return only JSON."),
            HumanMessage(content=prompt),
        ])
        content = self._message_to_text(response.content)
        match = re.search(r"\{.*\}", content, flags=re.S)
        if match:
            return json.loads(match.group(0))
        return {}

    def _build_session_summary_rules(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        user_texts = [message["content"] for message in messages if message["role"] == "user"]
        current_goal = user_texts[-1] if user_texts else ""
        return {
            "current_goal": self._trim_text(current_goal, 120),
            "objects_in_discussion": self._extract_objects(user_texts),
            "confirmed_facts": self._extract_confirmed_facts(user_texts),
            "attempted_actions": self._extract_attempted_actions(user_texts),
            "open_questions": self._extract_open_questions(user_texts),
            "constraints": self._extract_constraints(user_texts),
        }

    def _build_user_memory_summary(self, user_uuid: str) -> str:
        cached = self.memory_repository.get_user_memory_summary_text(user_uuid)
        if cached is not None:
            return cached

        memory_map = self.memory_repository.get_user_memory_map(user_uuid)
        if not memory_map:
            return ""

        label_map = {
            "owned_items": "Owned furniture/devices",
            "home_environment": "Home environment",
            "preferences": "User preferences",
            "avoidances": "User avoidances",
            "budget_preference": "Budget preference",
            "persistent_issues": "Persistent issues",
        }
        ordered_keys = [
            "owned_items",
            "home_environment",
            "preferences",
            "avoidances",
            "budget_preference",
            "persistent_issues",
        ]

        lines: list[str] = []
        for memory_key in ordered_keys:
            memory_entry = memory_map.get(memory_key)
            if not memory_entry:
                continue
            values = memory_entry["memory_value"][:12]
            if not values:
                continue
            lines.append(f"- {label_map[memory_key]}: {'; '.join(values)}")

        summary_text = "\n".join(lines)
        self.memory_repository.cache_user_memory_summary_text(user_uuid, summary_text)
        return summary_text

    def _format_session_summary(self, summary: dict[str, Any]) -> str:
        label_map = {
            "current_goal": "Current goal",
            "objects_in_discussion": "Objects in discussion",
            "confirmed_facts": "Confirmed facts",
            "attempted_actions": "Attempted actions",
            "open_questions": "Open questions",
            "constraints": "Constraints",
        }
        lines: list[str] = []
        for key in [
            "current_goal",
            "objects_in_discussion",
            "confirmed_facts",
            "attempted_actions",
            "open_questions",
            "constraints",
        ]:
            value = summary.get(key)
            if not value:
                continue
            if isinstance(value, list):
                lines.append(f"- {label_map[key]}: {'; '.join(value)}")
            else:
                lines.append(f"- {label_map[key]}: {value}")
        return "\n".join(lines)

    def _get_latest_user_message(self, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        for message in reversed(messages):
            if message["role"] == "user":
                return message
        return None

    def _extract_user_memory(self, text: str) -> dict[str, list[str]]:
        try:
            llm_result = self._extract_user_memory_llm(text)
            if llm_result:
                return llm_result
        except Exception as exc:
            logger.info("[memory] LLM user memory extraction failed, falling back to rules: %s", exc)
        return self._extract_user_memory_rules(text)

    def _extract_user_memory_llm(self, text: str) -> dict[str, list[str]]:
        from model.factory import memory_model

        prompt = f"""Extract stable long-term facts about the user. Return ONLY JSON with these optional keys (omit empty arrays):

- owned_items: furniture/devices the user explicitly owns or purchased
- home_environment: home conditions, location, pets, floor type, family
- preferences: things the user likes, prefers, or wants
- avoidances: things the user dislikes or wants to avoid
- budget_preference: any budget or price range mentioned
- persistent_issues: recurring problems with furniture/devices

Rules:
- Only extract EXPLICIT facts (e.g., "我家有猫", "买了布艺沙发", "预算3000")
- Do NOT extract vague wishes (e.g., "可能想要", "大概考虑")
- Each value should be a short Chinese phrase (max 20 chars)
- Return exactly the JSON, no extra text

User message:
{text}

JSON:"""
        response = memory_model.invoke([
            SystemMessage(content="You are a precise information extractor. Return only JSON."),
            HumanMessage(content=prompt),
        ])
        content = self._message_to_text(response.content)
        match = re.search(r"\{.*\}", content, flags=re.S)
        if match:
            result = json.loads(match.group(0))
            return {k: v for k, v in result.items() if isinstance(v, list) and v}
        return {}

    def _extract_user_memory_rules(self, text: str) -> dict[str, list[str]]:
        clauses = self._split_clauses(text)
        extracted: dict[str, list[str]] = {}
        for clause in clauses:
            if not self._looks_explicit_and_stable(clause):
                continue

            if self._contains_any(clause, ENVIRONMENT_KEYWORDS):
                extracted.setdefault("home_environment", []).append(clause)
            if self._contains_any(clause, FURNITURE_KEYWORDS) and self._contains_any(clause, ["我家", "家里", "我们家", "买了", "用的是"]):
                extracted.setdefault("owned_items", []).append(clause)
            if self._contains_any(clause, AVOIDANCE_KEYWORDS):
                extracted.setdefault("avoidances", []).append(clause)
            elif self._contains_any(clause, PREFERENCE_KEYWORDS):
                extracted.setdefault("preferences", []).append(clause)
            if "预算" in clause or re.search(r"\d+\s*(元|块|千|万)", clause):
                extracted.setdefault("budget_preference", []).append(clause)
            if self._contains_any(clause, ISSUE_KEYWORDS) and self._contains_any(clause, FURNITURE_KEYWORDS):
                extracted.setdefault("persistent_issues", []).append(clause)

        return {key: self._dedupe_preserve_order(values)[:6] for key, values in extracted.items()}

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

    def _extract_objects(self, texts: list[str]) -> list[str]:
        objects: list[str] = []
        for text in texts:
            for keyword in FURNITURE_KEYWORDS:
                if keyword in text:
                    objects.append(keyword)
        return self._dedupe_preserve_order(objects)[:8]

    def _extract_confirmed_facts(self, texts: list[str]) -> list[str]:
        facts: list[str] = []
        for clause in self._iter_clauses(texts):
            if self._looks_explicit_and_stable(clause):
                facts.append(clause)
        return self._dedupe_preserve_order(facts)[:8]

    def _extract_attempted_actions(self, texts: list[str]) -> list[str]:
        actions = [
            clause for clause in self._iter_clauses(texts)
            if self._contains_any(clause, ATTEMPT_KEYWORDS)
        ]
        return self._dedupe_preserve_order(actions)[:6]

    def _extract_open_questions(self, texts: list[str]) -> list[str]:
        questions = [
            self._trim_text(text.strip(), 120)
            for text in texts[-3:]
            if any(token in text for token in ["怎么", "如何", "吗", "能不能", "是否", "?", "？"])
        ]
        return self._dedupe_preserve_order(questions)[:3]

    def _extract_constraints(self, texts: list[str]) -> list[str]:
        constraints = [
            clause for clause in self._iter_clauses(texts)
            if (
                self._contains_any(clause, ENVIRONMENT_KEYWORDS)
                or self._contains_any(clause, PREFERENCE_KEYWORDS)
                or self._contains_any(clause, AVOIDANCE_KEYWORDS)
                or "预算" in clause
                or re.search(r"\d+\s*(元|块|千|万)", clause)
            )
        ]
        return self._dedupe_preserve_order(constraints)[:6]

    def _merge_memory_values(self, memory_key: str, current_values: list[str], new_values: list[str]) -> list[str]:
        if memory_key == "budget_preference":
            return new_values[-1:]
        merged = self._dedupe_preserve_order(current_values + new_values)
        return merged[-6:]

    def _iter_clauses(self, texts: list[str]):
        for text in texts:
            for clause in self._split_clauses(text):
                yield clause

    def _split_clauses(self, text: str) -> list[str]:
        raw_clauses = re.split(r"[，。；！？!?、\n]", text)
        return [self._trim_text(clause.strip(), 80) for clause in raw_clauses if clause and clause.strip()]

    def _looks_explicit_and_stable(self, clause: str) -> bool:
        if not clause:
            return False
        if any(token in clause for token in ["可能", "大概", "最近想", "也许", "打算"]):
            return False
        return self._contains_any(clause, EXPLICIT_FACT_MARKERS)

    def _contains_any(self, text: str, keywords: list[str]) -> bool:
        return any(keyword in text for keyword in keywords)

    def _dedupe_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _trim_text(self, text: str, max_len: int) -> str:
        return text if len(text) <= max_len else text[: max_len - 1] + "…"
