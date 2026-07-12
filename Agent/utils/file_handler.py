import hashlib
import os

from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_core.documents import Document

from utils.logger_handler import logger


def _hash_file(filepath: str, algorithm: str) -> str | None:
    if not os.path.exists(filepath):
        logger.error(f"[hash] file not found: {filepath}")
        return None
    if not os.path.isfile(filepath):
        logger.error(f"[hash] path is not a file: {filepath}")
        return None

    hash_obj = hashlib.new(algorithm)
    try:
        with open(filepath, "rb") as file_obj:
            while chunk := file_obj.read(4096):
                hash_obj.update(chunk)
    except Exception as exc:
        logger.error(f"[hash] failed for {filepath}: {exc}")
        return None

    return hash_obj.hexdigest()


def get_file_md5_hex(filepath: str) -> str | None:
    return _hash_file(filepath, "md5")


def get_file_sha256_hex(filepath: str) -> str | None:
    return _hash_file(filepath, "sha256")


def listdir_with_allowed_type(path: str, allowed_types: tuple[str, ...]) -> tuple[str, ...]:
    if not os.path.isdir(path):
        logger.error(f"[listdir_with_allowed_type] {path} is not a directory")
        return tuple()

    files = [
        os.path.join(path, file_name)
        for file_name in os.listdir(path)
        if file_name.endswith(allowed_types)
    ]
    return tuple(sorted(files))


def pdf_loader(filepath: str, passwd=None) -> list[Document]:
    return PyPDFLoader(filepath, passwd).load()


def txt_loader(filepath: str) -> list[Document]:
    return TextLoader(filepath, encoding="utf-8").load()
