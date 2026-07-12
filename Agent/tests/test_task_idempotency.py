import unittest
from unittest.mock import patch

from agent.task_service import TaskService


class FakeTaskRepository:
    def __init__(self):
        self.created_count = 0
        self.create_calls = 0
        self.activate_calls = 0
        self.patch_calls = 0
        self.events = set()
        self.tasks_by_request = {}
        self.tasks_by_id = {}
        self.request_by_task_id = {}
        self.resume_task = {
            "task_id": "task-1",
            "user_uuid": "user-1",
            "origin_session_id": "session-1",
            "active_session_id": "session-1",
            "topic": "chair question",
            "subject_type": "chair",
            "status": "active",
            "goal": "choose a chair",
            "next_action": "compare options",
            "state_version": 1,
            "last_message_id": 10,
            "facts": [],
        }

    def has_task_event(self, task_id, request_id):
        return (task_id, request_id) in self.events

    def create_task(
        self,
        user_uuid,
        session_id,
        topic,
        subject_type,
        goal,
        last_message_id,
        request_id=None,
    ):
        self.create_calls += 1
        if request_id and request_id in self.tasks_by_request:
            return self.tasks_by_request[request_id]

        self.created_count += 1
        task = {
            "task_id": f"task-{self.created_count}",
            "user_uuid": user_uuid,
            "origin_session_id": session_id,
            "active_session_id": session_id,
            "origin_request_id": request_id,
            "topic": topic,
            "subject_type": subject_type,
            "status": "active",
            "goal": goal,
            "next_action": None,
            "state_version": 1,
            "last_message_id": last_message_id,
            "facts": [],
        }
        if request_id:
            self.tasks_by_request[request_id] = task
            self.request_by_task_id[task["task_id"]] = request_id
        self.tasks_by_id[task["task_id"]] = task
        return task

    def activate_task(self, task_id, user_uuid, session_id, last_message_id):
        self.activate_calls += 1
        task = dict(self.resume_task)
        task["task_id"] = task_id
        task["user_uuid"] = user_uuid
        task["active_session_id"] = session_id
        task["last_message_id"] = last_message_id
        task["state_version"] = self.resume_task["state_version"] + 1
        self.tasks_by_id[task_id] = task
        return task

    def apply_patch(
        self,
        task_id,
        user_uuid,
        expected_version,
        request_id,
        source_message_id,
        patch,
    ):
        self.patch_calls += 1
        self.events.add((task_id, request_id))
        task = dict(self.resume_task)
        task["task_id"] = task_id
        task["user_uuid"] = user_uuid
        task["state_version"] = expected_version + 1
        task["last_message_id"] = source_message_id
        task["next_action"] = patch.get("task_updates", {}).get("next_action")
        self.tasks_by_id[task_id] = task
        stored_request_id = self.request_by_task_id.get(task_id)
        if stored_request_id:
            self.tasks_by_request[stored_request_id] = task
        return task


class TaskIdempotencyTest(unittest.TestCase):
    def test_duplicate_new_request_reuses_created_task(self):
        repository = FakeTaskRepository()
        service = TaskService(repository)
        route = {
            "action": "new",
            "task": None,
            "draft": {
                "topic": "chair question",
                "subject_type": "chair",
                "goal": "choose a chair",
            },
        }

        with patch.object(
            service,
            "_extract_patch",
            return_value={
                "task_updates": {"next_action": "compare wooden and fabric chairs"},
                "facts": [],
            },
        ):
            first = service.update_after_turn(
                route=route,
                query="I need a chair",
                assistant_message="Compare a few options.",
                session_id="session-1",
                user_uuid="user-1",
                request_id="request-1",
                user_message_id=11,
            )
            second = service.update_after_turn(
                route=route,
                query="I need a chair",
                assistant_message="Compare a few options.",
                session_id="session-1",
                user_uuid="user-1",
                request_id="request-1",
                user_message_id=11,
            )

        self.assertEqual(repository.created_count, 1)
        self.assertEqual(repository.patch_calls, 1)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertIn(("task-1", "request-1"), repository.events)

    def test_duplicate_resume_request_skips_activation(self):
        repository = FakeTaskRepository()
        service = TaskService(repository)
        route = {"action": "resume", "task": dict(repository.resume_task)}

        with patch.object(
            service,
            "_extract_patch",
            return_value={
                "task_updates": {"next_action": "check seat width"},
                "facts": [],
            },
        ):
            first = service.update_after_turn(
                route=route,
                query="Continue the chair task",
                assistant_message="Measure the seat width first.",
                session_id="session-1",
                user_uuid="user-1",
                request_id="request-2",
                user_message_id=12,
            )
            refreshed_route = {"action": "resume", "task": dict(repository.tasks_by_id["task-1"])}
            second = service.update_after_turn(
                route=refreshed_route,
                query="Continue the chair task",
                assistant_message="Measure the seat width first.",
                session_id="session-1",
                user_uuid="user-1",
                request_id="request-2",
                user_message_id=12,
            )

        self.assertEqual(repository.activate_calls, 1)
        self.assertEqual(repository.patch_calls, 1)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertIn(("task-1", "request-2"), repository.events)


if __name__ == "__main__":
    unittest.main()
