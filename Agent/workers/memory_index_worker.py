import os
import time

from agent.conversation_vector_store import ConversationVectorStore
from db.outbox_repository import OutboxRepository, TASK_TYPE_CONVERSATION_EPISODE
from utils.logger_handler import logger


class MemoryIndexWorker:
    def __init__(self):
        self.batch_size = int(os.getenv("MEMORY_WORKER_BATCH_SIZE", "20"))
        self.poll_seconds = float(os.getenv("MEMORY_WORKER_POLL_SECONDS", "2"))
        self.max_retries = int(os.getenv("MEMORY_WORKER_MAX_RETRIES", "8"))
        self.stale_after_seconds = int(
            os.getenv("MEMORY_WORKER_STALE_AFTER_SECONDS", "300")
        )
        self.backfill_on_start = (
            os.getenv("MEMORY_WORKER_BACKFILL_ON_START", "true").lower() == "true"
        )
        self.outbox_repository = OutboxRepository()
        self.vector_store = ConversationVectorStore()

    def run_forever(self) -> None:
        if self.backfill_on_start:
            inserted = self.outbox_repository.enqueue_missing_conversation_episodes()
            if inserted:
                logger.info(
                    "[memory worker] enqueued %s missing conversation episodes",
                    inserted,
                )

        recovered = self.outbox_repository.recover_stale_tasks(
            self.stale_after_seconds
        )
        if recovered:
            logger.warning("[memory worker] recovered %s stale tasks", recovered)

        logger.info(
            "[memory worker] started batch_size=%s poll_seconds=%s",
            self.batch_size,
            self.poll_seconds,
        )
        while True:
            try:
                processed = self.run_once()
            except Exception as exc:
                logger.error(
                    "[memory worker] polling failed: %s",
                    exc,
                    exc_info=True,
                )
                processed = 0
            if processed == 0:
                time.sleep(self.poll_seconds)

    def run_once(self) -> int:
        tasks = self.outbox_repository.claim_pending_tasks(self.batch_size)
        for task in tasks:
            try:
                self._process_task(task)
                self.outbox_repository.mark_completed(int(task["id"]))
            except Exception as exc:
                logger.warning(
                    "[memory worker] task_id=%s failed: %s",
                    task["id"],
                    exc,
                    exc_info=True,
                )
                self.outbox_repository.mark_failed(
                    task_id=int(task["id"]),
                    retry_count=int(task["retry_count"]),
                    error=exc,
                    max_retries=self.max_retries,
                )
        return len(tasks)

    def _process_task(self, task: dict) -> None:
        if task["task_type"] != TASK_TYPE_CONVERSATION_EPISODE:
            raise ValueError(f"Unsupported outbox task type: {task['task_type']}")

        episode = self.outbox_repository.get_conversation_episode(task)
        if episode is None:
            raise ValueError("Conversation episode messages are incomplete or inaccessible")
        self.vector_store.upsert_episode(episode)


def main() -> None:
    MemoryIndexWorker().run_forever()


if __name__ == "__main__":
    main()
