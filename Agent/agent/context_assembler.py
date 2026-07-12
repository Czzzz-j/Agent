from typing import Any

from db.memory_repository import MemoryRepository
from db.session_repository import SessionRepository


FACT_LABELS = {
    "confirmed_fact": "Confirmed",
    "constraint": "Constraint",
    "attempt": "Attempted",
    "result": "Result",
    "rejection": "Rejected",
    "open_question": "Open question",
}


class ContextAssembler:
    def __init__(
        self,
        session_repository: SessionRepository,
        memory_repository: MemoryRepository,
        recent_turns: int = 3,
        max_context_chars: int = 20000,
    ):
        self.session_repository = session_repository
        self.memory_repository = memory_repository
        self.recent_turns = recent_turns
        self.max_context_chars = max_context_chars

    def assemble(
        self,
        query: str,
        session_id: str,
        user_uuid: str,
        route: dict[str, Any],
        history_recall_context: str = "",
    ) -> dict[str, Any]:
        sections: list[tuple[int, str, str]] = []
        if "exact MySQL lookup" in history_recall_context:
            sections.append((1, "recalled_exact", history_recall_context))
            history_recall_context = ""

        session_memory = self.memory_repository.get_session_memory(session_id)
        if session_memory:
            summary_text = self._format_session_summary(session_memory["summary"])
            if summary_text:
                sections.append((2, "session_summary", summary_text))

        task = route.get("task")
        if task:
            task_context = self._format_task(task)
            if task_context:
                sections.append((2, "current_task", task_context))

        relevant_user_memory = self._select_user_memory(query, user_uuid, task)
        if relevant_user_memory:
            sections.append((4, "user_profile", relevant_user_memory))

        if history_recall_context:
            sections.append((5, "recalled_history", history_recall_context))

        system_context = self._fit_budget(sections)
        if route.get("action") in {"resume", "new"}:
            recent_history = []
        else:
            recent_history = self.session_repository.get_recent_history(
                session_id,
                user_uuid,
            )[-self.recent_turns * 2 :]
        return {
            "system_context": system_context,
            "recent_history": recent_history,
            "task_id": task.get("task_id") if task else None,
            "route_action": route.get("action"),
        }

    def _format_task(self, task: dict[str, Any]) -> str:
        lines = [
            "Current task state:",
            f"- Task ID: {task['task_id']}",
            f"- Topic: {task.get('topic') or 'unknown'}",
            f"- Status: {task.get('status') or 'active'}",
        ]
        if task.get("goal"):
            lines.append(f"- Goal: {self._trim(str(task['goal']), 500)}")
        if task.get("next_action"):
            lines.append(
                f"- Previous next action: {self._trim(str(task['next_action']), 400)}"
            )

        fact_limits = {
            "constraint": 5,
            "rejection": 3,
            "confirmed_fact": 5,
            "attempt": 3,
            "result": 3,
            "open_question": 3,
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for fact in task.get("facts", []):
            grouped.setdefault(fact["fact_type"], []).append(fact)

        for fact_type in [
            "constraint",
            "rejection",
            "confirmed_fact",
            "attempt",
            "result",
            "open_question",
        ]:
            facts = grouped.get(fact_type, [])[: fact_limits[fact_type]]
            if not facts:
                continue
            rendered = []
            for fact in facts:
                value = fact.get("value")
                if isinstance(value, list):
                    value_text = "; ".join(str(item) for item in value[:3])
                else:
                    value_text = str(value)
                rendered.append(
                    f"{self._trim(value_text, 240)} "
                    f"(source_message_id={fact['source_message_id']})"
                )
            lines.append(f"- {FACT_LABELS[fact_type]}: {' | '.join(rendered)}")
        return "\n".join(lines)

    def _select_user_memory(
        self,
        query: str,
        user_uuid: str,
        task: dict[str, Any] | None,
    ) -> str:
        memory_map = self.memory_repository.get_user_memory_map(user_uuid)
        if not memory_map:
            return ""

        topic_text = " ".join(
            [
                query,
                str(task.get("topic", "")) if task else "",
                str(task.get("goal", "")) if task else "",
            ]
        )
        candidates: list[tuple[int, str, str]] = []
        for memory_key, entry in memory_map.items():
            values = entry.get("memory_value", [])
            for value in values:
                value_text = str(value)
                score = self._overlap_score(topic_text, value_text)
                if memory_key in {"avoidances", "home_environment"}:
                    score += 1
                candidates.append((score, memory_key, value_text))

        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = [candidate for candidate in candidates if candidate[0] > 0][:3]
        if not selected:
            return ""
        return "\n".join(
            f"- {memory_key}: {self._trim(value, 240)}"
            for _, memory_key, value in selected
        )

    def _fit_budget(self, sections: list[tuple[int, str, str]]) -> str:
        if not sections:
            return ""
        sections = sorted(sections, key=lambda x: x[0])
        weights = {1: 0.30, 2: 0.45, 3: 0.10, 4: 0.10, 5: 0.05}
        result: list[str] = []
        for priority, tag, content in sections:
            allocation = int(self.max_context_chars * weights.get(priority, 0.05))
            trimmed = content[:allocation]
            if trimmed:
                result.append(f"<{tag}>\n{trimmed}\n</{tag}>")
        return "\n\n".join(result)

    @staticmethod
    def _overlap_score(left: str, right: str) -> int:
        def bigrams(value: str) -> set[str]:
            compact = "".join(
                char for char in value if "\u4e00" <= char <= "\u9fff"
            )
            return {
                compact[index : index + 2]
                for index in range(max(0, len(compact) - 1))
            }

        return len(bigrams(left) & bigrams(right))

    @staticmethod
    def _format_session_summary(summary: dict[str, Any]) -> str:
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
                lines.append(f"- {label_map[key]}: {'; '.join(str(v) for v in value)}")
            else:
                lines.append(f"- {label_map[key]}: {value}")
        return "\n".join(lines)

    @staticmethod
    def _trim(text: str, limit: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."
