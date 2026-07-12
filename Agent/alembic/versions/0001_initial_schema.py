from __future__ import annotations

from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


TABLES_IN_DROP_ORDER = [
    "conversation_task_events",
    "conversation_task_facts",
    "conversation_tasks",
    "knowledge_index_state",
    "knowledge_index_outbox",
    "knowledge_chunks",
    "knowledge_document_versions",
    "knowledge_documents",
    "memory_index_outbox",
    "user_memory",
    "session_memory",
    "chat_messages",
    "chat_sessions",
    "feedbacks",
    "external_records",
    "users",
]


def upgrade() -> None:
    schema_path = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    sql_text = schema_path.read_text(encoding="utf-8")
    for statement in _iter_statements(sql_text):
        op.execute(statement)


def downgrade() -> None:
    op.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table_name in TABLES_IN_DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS `{table_name}`")
    op.execute("SET FOREIGN_KEY_CHECKS = 1")


def _iter_statements(sql_text: str):
    statement_lines: list[str] = []
    for raw_line in sql_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        upper = line.upper()
        if upper.startswith("CREATE DATABASE") or upper.startswith("USE "):
            continue
        statement_lines.append(raw_line)
        if line.endswith(";"):
            statement = "\n".join(statement_lines).strip().rstrip(";")
            statement_lines = []
            if statement:
                yield statement
    tail = "\n".join(statement_lines).strip().rstrip(";")
    if tail:
        yield tail

