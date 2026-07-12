import os

import yaml
from dotenv import load_dotenv

from utils.path_tool import get_abs_path


load_dotenv(override=False)


def _load_yaml(config_path: str, encoding: str = "utf-8"):
    with open(config_path, "r", encoding=encoding) as file_obj:
        return yaml.load(file_obj, Loader=yaml.FullLoader)


def load_rag_config(config_path: str = get_abs_path("config/rag.yml"), encoding: str = "utf-8"):
    return _load_yaml(config_path, encoding)


def load_agent_config(config_path: str = get_abs_path("config/agent.yml"), encoding: str = "utf-8"):
    return _load_yaml(config_path, encoding)


def load_prompts_config(config_path: str = get_abs_path("config/prompts.yml"), encoding: str = "utf-8"):
    return _load_yaml(config_path, encoding)


def load_chroma_config(config_path: str = get_abs_path("config/chroma.yml"), encoding: str = "utf-8"):
    if os.getenv("CHROMA_HOST"):
        return {
            "host": os.getenv("CHROMA_HOST"),
            "port": int(os.getenv("CHROMA_PORT", 8000)),
            "collection_name": os.getenv("CHROMA_COLLECTION", "knowledge_v2"),
            "conversation_collection_name": os.getenv("CONVERSATION_MEMORY_COLLECTION", "conversation_memory_v1"),
            "persist_directory": os.getenv("CHROMA_PERSIST_DIR", "./chroma_data"),
            "k": int(os.getenv("CHROMA_K", 20)),
            "data_path": os.getenv("CHROMA_DATA_PATH", "data"),
            "allow_knowledge_file_type": os.getenv("CHROMA_ALLOW_TYPES", "txt,pdf").split(","),
            "chunk_size": int(os.getenv("CHROMA_CHUNK_SIZE", 500)),
            "chunk_overlap": int(os.getenv("CHROMA_CHUNK_OVERLAP", 80)),
            "separators": os.getenv("CHROMA_SEPARATORS", "\n\n,\n,。！？.,!? ").split(","),
        }
    return _load_yaml(config_path, encoding)


def load_mysql_config():
    if os.getenv("MYSQL_HOST"):
        return {
            "host": os.getenv("MYSQL_HOST"),
            "port": int(os.getenv("MYSQL_PORT", 3306)),
            "user": os.getenv("MYSQL_USER", "root"),
            "password": os.getenv("MYSQL_PASSWORD", ""),
            "database": os.getenv("MYSQL_DATABASE", "furniture_agent"),
            "pool_name": "agent_pool",
            "pool_size": int(os.getenv("MYSQL_POOL_SIZE", 10)),
        }
    return _load_yaml(get_abs_path("config/mysql.yml"))


def load_redis_config(config_path: str = get_abs_path("config/redis.yml"), encoding: str = "utf-8"):
    if os.getenv("REDIS_HOST"):
        return {
            "host": os.getenv("REDIS_HOST"),
            "port": int(os.getenv("REDIS_PORT", 6379)),
            "db": int(os.getenv("REDIS_DB", 0)),
            "socket_timeout": int(os.getenv("REDIS_SOCKET_TIMEOUT", 5)),
        }
    return _load_yaml(config_path, encoding)


rag_conf = load_rag_config()
agent_conf = load_agent_config()
prompts_conf = load_prompts_config()
chroma_conf = load_chroma_config()
mysql_conf = load_mysql_config()
redis_conf = load_redis_config()
