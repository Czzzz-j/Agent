from db.outbox_repository import OutboxRepository
from utils.logger_handler import logger


def main() -> None:
    repository = OutboxRepository()
    inserted = repository.enqueue_missing_conversation_episodes()
    requeued = repository.requeue_all_conversation_episodes()
    logger.info(
        "[memory rebuild] inserted=%s requeued=%s; the memory worker will rebuild Chroma",
        inserted,
        requeued,
    )


if __name__ == "__main__":
    main()
