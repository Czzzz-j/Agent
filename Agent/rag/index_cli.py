import argparse
import os

from db.knowledge_repository import KnowledgeRepository
from rag.document_parser import PARSER_VERSION
from rag.vector_store import KnowledgeVectorStore
from utils.config_handler import chroma_conf, rag_conf
from utils.path_tool import get_abs_path


def build_repository() -> KnowledgeRepository:
    repository = KnowledgeRepository()
    repository.ensure_schema_state(chroma_conf["collection_name"])
    return repository


def sync_command() -> int:
    repository = build_repository()
    source_root = get_abs_path(chroma_conf["data_path"])
    allowed_types = tuple(f".{suffix.strip('.')}" for suffix in chroma_conf["allow_knowledge_file_type"])
    file_paths = []
    for file_name in sorted(os.listdir(source_root)):
        absolute_path = os.path.join(source_root, file_name)
        if os.path.isfile(absolute_path) and absolute_path.endswith(allowed_types):
            file_paths.append(absolute_path)
    result = repository.sync_source_files(
        source_root=source_root,
        file_paths=file_paths,
        parser_version=PARSER_VERSION,
        embedding_version=rag_conf["embedding_model_name"],
    )
    print(result)
    return 0


def rebuild_command() -> int:
    repository = build_repository()
    source_root = get_abs_path(chroma_conf["data_path"])
    queued = repository.rebuild_all(
        source_root=source_root,
        parser_version=PARSER_VERSION,
        embedding_version=rag_conf["embedding_model_name"],
    )
    print({"queued": queued})
    return 0


def status_command() -> int:
    repository = build_repository()
    health = repository.get_health(chroma_conf["collection_name"])
    try:
        vector_store = KnowledgeVectorStore(
            collection_name=chroma_conf["collection_name"],
            require_persistent=True,
        )
        health["vector_chunks"] = vector_store.count()
        health["index_consistent"] = health["active_chunks"] == health["vector_chunks"]
    except Exception as exc:
        health["vector_chunks"] = None
        health["index_consistent"] = False
        health["vector_error"] = str(exc)
    print(health)
    return 0


def audit_command() -> int:
    repository = build_repository()
    mysql_count = repository.get_active_chunk_count()
    vector_count = None
    vector_error = None
    try:
        vector_store = KnowledgeVectorStore(
            collection_name=chroma_conf["collection_name"],
            require_persistent=True,
        )
        vector_count = vector_store.count()
    except Exception as exc:
        vector_error = str(exc)

    consistent = vector_count is not None and mysql_count == vector_count
    queued = 0
    if not consistent:
        queued = repository.enqueue_rebuild()
        repository.refresh_health(chroma_conf["collection_name"], status="degraded")
    print(
        {
            "mysql_active_chunks": mysql_count,
            "chroma_chunks": vector_count,
            "consistent": consistent,
            "vector_error": vector_error,
            "rebuild_tasks_queued": queued,
        }
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description="Knowledge index maintenance CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync")
    subparsers.add_parser("rebuild")
    subparsers.add_parser("status")
    subparsers.add_parser("audit")
    args = parser.parse_args()

    if args.command == "sync":
        raise SystemExit(sync_command())
    if args.command == "rebuild":
        raise SystemExit(rebuild_command())
    if args.command == "audit":
        raise SystemExit(audit_command())
    raise SystemExit(status_command())


if __name__ == "__main__":
    main()
