import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from agent.query_rewriter import QueryRewriter


class QueryRewriterTest(unittest.TestCase):
    def setUp(self):
        self.sofa_task = {
            "task_id": "task-sofa",
            "topic": "布艺沙发",
            "goal": "去除异味并保证宠物安全",
        }
        self.empty_task: dict = {}

    def test_pure_referential_it_replaced_by_topic(self):
        rewriter = QueryRewriter(model=None)
        result = rewriter.rewrite("它", task=self.sofa_task)
        self.assertEqual(result, "布艺沙发")

    def test_pure_referential_that_replaced_by_topic(self):
        rewriter = QueryRewriter(model=None)
        result = rewriter.rewrite("那个", task=self.sofa_task)
        self.assertEqual(result, "布艺沙发")

    def test_pure_referential_without_topic_returns_original(self):
        rewriter = QueryRewriter(model=None)
        result = rewriter.rewrite("它", task=self.empty_task)
        self.assertEqual(result, "它")

    def test_punctuation_only_not_referential(self):
        rewriter = QueryRewriter(model=None)
        result = rewriter.rewrite("？", task=self.sofa_task)
        self.assertEqual(result, "？")

    def test_referential_with_content_triggers_llm(self):
        rewriter = QueryRewriter(model=None)
        rewriter._model_initialized = True
        fake_model = MagicMock()
        fake_model.invoke.return_value = AIMessage(content="布艺沙发还有什么材质可选")
        rewriter._model = fake_model

        result = rewriter.rewrite(
            "那它还有什么材质可选",
            task=self.sofa_task,
        )
        self.assertEqual(result, "布艺沙发还有什么材质可选")

    def test_concrete_query_returns_as_is(self):
        rewriter = QueryRewriter(model=None)
        result = rewriter.rewrite("布艺沙发怎么清洁", task=self.sofa_task)
        self.assertEqual(result, "布艺沙发怎么清洁")

    def test_greeting_returns_as_is(self):
        rewriter = QueryRewriter(model=None)
        result = rewriter.rewrite("你好", task=self.sofa_task)
        self.assertEqual(result, "你好")

    def test_llm_failure_falls_back_to_concat(self):
        rewriter = QueryRewriter(model=None)
        rewriter._model_initialized = True
        fake_model = MagicMock()
        fake_model.invoke.side_effect = RuntimeError("API error")
        rewriter._model = fake_model

        result = rewriter.rewrite(
            "那它怎么修",
            task=self.sofa_task,
        )
        self.assertEqual(result, "那它怎么修（布艺沙发）")

    def test_llm_empty_response_falls_back_to_concat(self):
        rewriter = QueryRewriter(model=None)
        rewriter._model_initialized = True
        fake_model = MagicMock()
        fake_model.invoke.return_value = AIMessage(content="")
        rewriter._model = fake_model

        result = rewriter.rewrite(
            "那个东西怎么处理",
            task=self.sofa_task,
        )
        self.assertEqual(result, "那个东西怎么处理（布艺沙发）")

    def test_empty_query_returns_empty(self):
        rewriter = QueryRewriter(model=None)
        result = rewriter.rewrite("", task=self.sofa_task)
        self.assertEqual(result, "")

    def test_none_query_returns_empty(self):
        rewriter = QueryRewriter(model=None)
        result = rewriter.rewrite(None, task=self.sofa_task)  # type: ignore[arg-type]
        self.assertEqual(result, "")

    def test_this_is_pure_referential(self):
        rewriter = QueryRewriter(model=None)
        result = rewriter.rewrite("这个", task=self.sofa_task)
        self.assertEqual(result, "布艺沙发")


if __name__ == "__main__":
    unittest.main()
