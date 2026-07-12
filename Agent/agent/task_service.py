import json
import math
import re
from collections import defaultdict
from typing import Any

from db.task_repository import TaskConflictError, TaskRepository
from model.factory import chat_model, embed_model
from utils.logger_handler import logger


SUBJECT_KEYWORDS = {
    "sofa": ["沙发", "布艺", "皮沙发", "坐垫", "海绵", "填充层", "异味"],
    "bed": ["床", "床垫", "床架"],
    "table": ["餐桌", "桌子", "茶几", "书桌", "桌面"],
    "cabinet": ["衣柜", "橱柜", "鞋柜", "书柜", "电视柜", "柜子"],
    "floor": ["地板", "木地板", "地砖", "地毯"],
    "robot_vacuum": [
        "扫地机器人",
        "扫拖机器人",
        "滚刷",
        "边刷",
        "尘盒",
        "拖布",
        "基站",
        "雷达",
        "回充",
    ],
}
TASK_SIGNAL_WORDS = [
    "怎么",
    "如何",
    "为什么",
    "故障",
    "异味",
    "污渍",
    "划痕",
    "清洁",
    "保养",
    "维修",
    "选购",
    "推荐",
    "卡住",
    "失灵",
    "受潮",
]
INTENT_KEYWORDS = {
    "purchase": ["选购", "推荐", "预算", "买哪款", "哪个好", "怎么选"],
    "cleaning": ["清洁", "清洗", "保养", "去污", "除味", "打理"],
    "troubleshooting": [
        "故障",
        "不工作",
        "失灵",
        "卡住",
        "报错",
        "异响",
        "异味",
        "划痕",
        "受潮",
        "怎么办",
        "为什么",
    ],
}
CONTINUATION_WORDS = [
    "接下来",
    "然后呢",
    "继续",
    "还是",
    "这个问题",
    "那个问题",
    "之前那个",
    "按你说的",
    "试过了",
    "检查过了",
]
STATE_SIGNAL_WORDS = [
    "已经",
    "试过",
    "用了",
    "检查过",
    "发现",
    "还是",
    "仍然",
    "改善",
    "没用",
    "不行",
    "解决了",
    "好了",
    "不要",
    "不想",
    "避免",
    "必须",
    "只能",
    "不能",
]
SMALL_TALK_PATTERNS = [
    r"^(你好|您好|在吗|谢谢|好的|好吧|明白了|知道了|再见)[！!。. ]*$",
    r"^(哈哈|呵呵|嗯+|哦+)[！!。. ]*$",
]


class TaskService:
    def __init__(self, task_repository: TaskRepository):
        self.task_repository = task_repository

    def route(
        self,
        query: str,
        session_id: str,
        user_uuid: str,
    ) -> dict[str, Any]:
        normalized = self._normalize(query)
        if self._is_small_talk(normalized):
            return {"action": "no_task", "task": None, "confidence": 1.0}

        active_task = self.task_repository.get_active_task(user_uuid, session_id)
        tasks = self.task_repository.list_user_tasks(user_uuid)
        subject_type = self._detect_subject(normalized)
        query_intent = self._detect_intent(normalized)

        if (
            active_task
            and self._looks_like_continuation(normalized)
            and (
                subject_type is None
                or active_task.get("subject_type") == subject_type
            )
        ):
            task = self.task_repository.get_task_with_facts(
                active_task["task_id"],
                user_uuid,
            )
            return {"action": "continue", "task": task, "confidence": 0.95}

        scored = [
            (
                self._task_score(
                    normalized,
                    subject_type,
                    query_intent,
                    task,
                ),
                task,
            )
            for task in tasks
        ]
        scored.sort(key=lambda item: item[0], reverse=True)

        if scored and scored[0][0] >= 0.62:
            best_score, best_task = scored[0]
            task = self.task_repository.get_task_with_facts(
                best_task["task_id"],
                user_uuid,
            )
            action = (
                "continue"
                if active_task and active_task["task_id"] == best_task["task_id"]
                else "resume"
            )
            return {"action": action, "task": task, "confidence": best_score}

        if (
            scored
            and scored[0][0] >= 0.42
            and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.12)
        ):
            best_score, best_task = scored[0]
            task = self.task_repository.get_task_with_facts(
                best_task["task_id"],
                user_uuid,
            )
            return {"action": "resume", "task": task, "confidence": best_score}

        if tasks and self._is_business_query(normalized, subject_type):
            compatible_tasks = [
                task
                for task in tasks
                if self._intents_compatible(
                    query_intent,
                    self._detect_intent(
                        " ".join(
                            str(task.get(field) or "")
                            for field in ["topic", "goal", "next_action"]
                        )
                    ),
                )
            ]
            semantic_ranked = self._semantic_rank(normalized, compatible_tasks)
            if (
                semantic_ranked
                and semantic_ranked[0][0] >= 0.78
                and (
                    len(semantic_ranked) == 1
                    or semantic_ranked[0][0] - semantic_ranked[1][0] >= 0.05
                )
            ):
                best_score, best_task = semantic_ranked[0]
                task = self.task_repository.get_task_with_facts(
                    best_task["task_id"],
                    user_uuid,
                )
                return {
                    "action": "resume",
                    "task": task,
                    "confidence": best_score,
                }

        if self._is_business_query(normalized, subject_type):
            return {
                "action": "new",
                "task": None,
                "confidence": 0.85,
                "draft": {
                    "topic": self._build_topic(normalized, subject_type),
                    "subject_type": subject_type,
                    "goal": self._trim(normalized, 300),
                },
            }

        if active_task and not subject_type:
            task = self.task_repository.get_task_with_facts(
                active_task["task_id"],
                user_uuid,
            )
            return {"action": "continue", "task": task, "confidence": 0.55}

        return {"action": "no_task", "task": None, "confidence": 0.75}

    def update_after_turn(
        self,
        route: dict[str, Any],
        query: str,
        assistant_message: str,
        session_id: str,
        user_uuid: str,
        request_id: str,
        user_message_id: int,
    ) -> dict[str, Any] | None:
        action = route.get("action")
        if action == "no_task":
            return None

        task = route.get("task")
        has_task_event = getattr(self.task_repository, "has_task_event", None)
        if task and callable(has_task_event) and has_task_event(task["task_id"], request_id):
            return task

        if action == "new":
            draft = route.get("draft", {})
            task = self.task_repository.create_task(
                user_uuid=user_uuid,
                session_id=session_id,
                topic=draft.get("topic") or self._trim(query, 200),
                subject_type=draft.get("subject_type"),
                goal=draft.get("goal") or self._trim(query, 300),
                last_message_id=user_message_id,
                request_id=request_id,
            )
        elif action == "resume" and task:
            task = self.task_repository.activate_task(
                task_id=task["task_id"],
                user_uuid=user_uuid,
                session_id=session_id,
                last_message_id=user_message_id,
            )

        if not task:
            return None

        if callable(has_task_event) and has_task_event(task["task_id"], request_id):
            return task

        should_extract = action in {"new", "resume"} or self._has_state_signal(query)
        if should_extract:
            patch = self._extract_patch(query, assistant_message, task)
        else:
            patch = {
                "task_updates": {
                    "next_action": self._extract_first_action(assistant_message)
                },
                "facts": [],
            }

        try:
            return self.task_repository.apply_patch(
                task_id=task["task_id"],
                user_uuid=user_uuid,
                expected_version=int(task["state_version"]),
                request_id=request_id,
                source_message_id=user_message_id,
                patch=patch,
            )
        except TaskConflictError:
            current = self.task_repository.get_task_with_facts(
                task["task_id"],
                user_uuid,
            )
            if not current:
                return None
            return self.task_repository.apply_patch(
                task_id=current["task_id"],
                user_uuid=user_uuid,
                expected_version=int(current["state_version"]),
                request_id=request_id,
                source_message_id=user_message_id,
                patch=patch,
            )

    def _extract_patch(
        self,
        query: str,
        assistant_message: str,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = f"""
You update a customer-service task state. Return JSON only.

Current task:
{json.dumps(self._task_for_prompt(task), ensure_ascii=False, default=str)}

Latest user message:
{query}

Assistant answer:
{assistant_message}

Rules:
- Only user statements can become confirmed facts, constraints, attempts, results, or rejections.
- An assistant recommendation is not an attempted action.
- Keep every value short and factual.
- Use only these fact_type values:
  confirmed_fact, constraint, attempt, result, rejection, open_question.
- task_updates may contain topic, subject_type, goal, next_action, status.
- status may be active, paused, resolved, or abandoned.

JSON shape:
{{
  "task_updates": {{
    "topic": null,
    "subject_type": null,
    "goal": null,
    "next_action": null,
    "status": null
  }},
  "facts": [
    {{"fact_type": "attempt", "value": "...", "confidence": 0.9}}
  ]
}}
""".strip()
        try:
            response = chat_model.invoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)
            parsed = self._parse_json_object(content)
            return self._validate_patch(parsed)
        except Exception as exc:
            logger.warning("[task state] model extraction failed: %s", exc)
            return self._heuristic_patch(query, assistant_message)

    def _heuristic_patch(
        self,
        query: str,
        assistant_message: str,
    ) -> dict[str, Any]:
        facts: dict[str, list[str]] = defaultdict(list)
        clauses = [
            clause.strip()
            for clause in re.split(r"[，。；！？!?\n]", query)
            if clause.strip()
        ]
        for clause in clauses:
            if any(word in clause for word in ["不要", "不想", "避免", "不能"]):
                facts["constraint"].append(clause)
                facts["rejection"].append(clause)
            if any(word in clause for word in ["已经", "试过", "用了", "检查过"]):
                facts["attempt"].append(clause)
            if any(word in clause for word in ["发现", "改善", "还是", "仍然", "没用", "不行"]):
                facts["result"].append(clause)
            if any(word in clause for word in ["我家", "家里", "用的是", "买了"]):
                facts["confirmed_fact"].append(clause)

        status = "resolved" if any(word in query for word in ["解决了", "好了", "恢复正常"]) else None
        next_action = self._extract_first_action(assistant_message)
        return {
            "task_updates": {
                "next_action": next_action,
                "status": status,
            },
            "facts": [
                {
                    "fact_type": fact_type,
                    "value": values[:3],
                    "confidence": 0.85,
                }
                for fact_type, values in facts.items()
                if values
            ],
        }

    def _task_score(
        self,
        query: str,
        subject_type: str | None,
        query_intent: str | None,
        task: dict[str, Any],
    ) -> float:
        score = 0.0
        if subject_type and task.get("subject_type") == subject_type:
            score += 0.62

        task_text = " ".join(
            str(task.get(field) or "")
            for field in ["topic", "subject_type", "goal", "next_action"]
        )
        task_intent = self._detect_intent(task_text)
        if not self._intents_compatible(query_intent, task_intent):
            score -= 0.35
        score += min(0.30, self._bigram_similarity(query, task_text) * 0.60)
        if any(word in query for word in ["之前", "上次", "回到", "继续"]):
            score += 0.10
        if task.get("status") == "active":
            score += 0.05
        elif task.get("status") == "resolved":
            score -= 0.10
        return min(score, 1.0)

    def _semantic_rank(
        self,
        query: str,
        tasks: list[dict[str, Any]],
    ) -> list[tuple[float, dict[str, Any]]]:
        documents = [
            " ".join(
                str(task.get(field) or "")
                for field in ["topic", "subject_type", "goal", "next_action"]
            )
            for task in tasks
        ]
        try:
            vectors = embed_model.embed_documents([query, *documents])
            query_vector = vectors[0]
            ranked = [
                (self._cosine_similarity(query_vector, vector), task)
                for vector, task in zip(vectors[1:], tasks)
            ]
            ranked.sort(key=lambda item: item[0], reverse=True)
            return ranked
        except Exception as exc:
            logger.info("[task router] embedding fallback unavailable: %s", exc)
            return []

    def _validate_patch(self, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict):
            return {"task_updates": {}, "facts": []}
        task_updates = patch.get("task_updates")
        if not isinstance(task_updates, dict):
            task_updates = {}

        grouped: dict[str, list[Any]] = defaultdict(list)
        confidences: dict[str, float] = {}
        for fact in patch.get("facts", []):
            if not isinstance(fact, dict):
                continue
            fact_type = fact.get("fact_type")
            value = fact.get("value")
            if fact_type not in {
                "confirmed_fact",
                "constraint",
                "attempt",
                "result",
                "rejection",
                "open_question",
            }:
                continue
            if value in (None, "", [], {}):
                continue
            values = value if isinstance(value, list) else [value]
            grouped[fact_type].extend(values)
            try:
                confidences[fact_type] = max(
                    confidences.get(fact_type, 0.0),
                    float(fact.get("confidence", 0.90)),
                )
            except (TypeError, ValueError):
                confidences[fact_type] = 0.90

        return {
            "task_updates": {
                key: value
                for key, value in task_updates.items()
                if key in {"topic", "subject_type", "goal", "next_action", "status"}
            },
            "facts": [
                {
                    "fact_type": fact_type,
                    "value": values[:5],
                    "confidence": confidences.get(fact_type, 0.90),
                }
                for fact_type, values in grouped.items()
            ],
        }

    @staticmethod
    def _parse_json_object(content: Any) -> dict[str, Any]:
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        text = str(content).strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Model did not return a JSON object")
        return json.loads(text[start : end + 1])

    @staticmethod
    def _task_for_prompt(task: dict[str, Any]) -> dict[str, Any]:
        return {
            "topic": task.get("topic"),
            "subject_type": task.get("subject_type"),
            "status": task.get("status"),
            "goal": task.get("goal"),
            "next_action": task.get("next_action"),
            "facts": [
                {
                    "fact_type": fact.get("fact_type"),
                    "value": fact.get("value"),
                }
                for fact in task.get("facts", [])[:12]
            ],
        }

    @staticmethod
    def _extract_first_action(answer: str) -> str | None:
        for line in answer.splitlines():
            cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
            if cleaned:
                return cleaned[:300]
        return None

    @staticmethod
    def _detect_subject(text: str) -> str | None:
        for subject_type, keywords in SUBJECT_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return subject_type
        return None

    @staticmethod
    def _detect_intent(text: str) -> str | None:
        for intent, keywords in INTENT_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return intent
        return None

    @staticmethod
    def _intents_compatible(left: str | None, right: str | None) -> bool:
        return left is None or right is None or left == right

    @staticmethod
    def _build_topic(query: str, subject_type: str | None) -> str:
        label_map = {
            "sofa": "沙发问题",
            "bed": "床具问题",
            "table": "桌面与桌具问题",
            "cabinet": "柜体问题",
            "floor": "地面问题",
            "robot_vacuum": "扫地机器人问题",
        }
        label = label_map.get(subject_type)
        return f"{label}：{query[:120]}" if label else query[:200]

    @staticmethod
    def _is_business_query(text: str, subject_type: str | None) -> bool:
        return bool(subject_type) or any(word in text for word in TASK_SIGNAL_WORDS)

    @staticmethod
    def _looks_like_continuation(text: str) -> bool:
        return any(word in text for word in CONTINUATION_WORDS)

    @staticmethod
    def _has_state_signal(text: str) -> bool:
        return any(word in text for word in STATE_SIGNAL_WORDS)

    @staticmethod
    def _is_small_talk(text: str) -> bool:
        return any(re.match(pattern, text) for pattern in SMALL_TALK_PATTERNS)

    @staticmethod
    def _bigram_similarity(left: str, right: str) -> float:
        def grams(value: str) -> set[str]:
            compact = re.sub(r"[\W_]+", "", value)
            return {
                compact[index : index + 2]
                for index in range(max(0, len(compact) - 1))
            }

        left_grams = grams(left)
        right_grams = grams(right)
        if not left_grams or not right_grams:
            return 0.0
        return len(left_grams & right_grams) / len(left_grams | right_grams)

    @staticmethod
    def _cosine_similarity(left: list[float], right: list[float]) -> float:
        denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(
            sum(v * v for v in right)
        )
        if not denominator:
            return 0.0
        return sum(a * b for a, b in zip(left, right)) / denominator

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.split()).strip()

    @staticmethod
    def _trim(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[: limit - 3] + "..."
