import os
import re
from hashlib import sha256

from utils.file_handler import pdf_loader, txt_loader

PARSER_VERSION = "v2"


def infer_domain_and_category(source_name: str) -> tuple[str, str]:
    lower_name = source_name.lower()
    if "扫地" in source_name or "扫拖" in source_name or "robot" in lower_name:
        return "robot_vacuum", _infer_robot_category(source_name)
    return "furniture", os.path.splitext(source_name)[0]


def _infer_robot_category(source_name: str) -> str:
    if "故障" in source_name:
        return "troubleshooting"
    if "维护" in source_name or "保养" in source_name:
        return "maintenance"
    if "选购" in source_name:
        return "buying_guide"
    return "general"


def parse_document(file_path: str) -> list[dict]:
    source_name = os.path.basename(file_path)
    suffix = os.path.splitext(source_name)[1].lower()
    if suffix == ".txt":
        text = "\n".join(doc.page_content for doc in txt_loader(file_path))
        return parse_text_document(text, source_name)
    if suffix == ".pdf":
        return parse_pdf_document(file_path, source_name)
    raise ValueError(f"unsupported file type: {file_path}")


def parse_text_document(text: str, source_name: str) -> list[dict]:
    faq_chunks = _split_faq_chunks(text)
    if faq_chunks:
        return faq_chunks

    numbered_chunks = _split_numbered_chunks(text)
    if numbered_chunks:
        return numbered_chunks

    return _split_plain_chunks(text)


def parse_pdf_document(file_path: str, source_name: str) -> list[dict]:
    parsed = []
    for doc in pdf_loader(file_path):
        page = doc.metadata.get("page", 0) + 1
        page_chunks = parse_text_document(doc.page_content, source_name)
        for chunk in page_chunks:
            chunk["page"] = page
            if not chunk.get("section"):
                chunk["section"] = f"page-{page}"
        parsed.extend(page_chunks)
    return parsed


def chunk_metadata(
    document_id: str,
    version_id: str,
    source_name: str,
    domain: str,
    category: str,
    embedding_version: str,
    chunks: list[dict],
) -> list[dict]:
    metadata_rows = []
    for index, chunk in enumerate(chunks):
        content = chunk["content"].strip()
        metadata_rows.append(
            {
                "chunk_id": f"{document_id}:{version_id}:{index}",
                "document_id": document_id,
                "version_id": version_id,
                "source_name": source_name,
                "domain": domain,
                "category": category,
                "intent": chunk.get("intent", "general"),
                "section": chunk.get("section", ""),
                "page": int(chunk.get("page", 0) or 0),
                "chunk_index": index,
                "content_hash": sha256(content.encode("utf-8")).hexdigest(),
                "parser_version": PARSER_VERSION,
                "embedding_version": embedding_version,
            }
        )
    return metadata_rows


def _split_faq_chunks(text: str) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    chunks = []
    current_question = None
    current_answer: list[str] = []

    def flush():
        if current_question and current_answer:
            chunks.append(
                {
                    "content": f"{current_question}\n{' '.join(current_answer)}".strip(),
                    "section": current_question[:40],
                    "intent": _infer_intent(current_question),
                    "keywords": extract_keywords(f"{current_question} {' '.join(current_answer)}"),
                }
            )

    for line in lines:
        if _is_question_line(line):
            flush()
            current_question = line
            current_answer = []
        elif current_question:
            current_answer.append(line)
    flush()
    return chunks


def _split_numbered_chunks(text: str) -> list[dict]:
    lines = [line.rstrip() for line in text.splitlines()]
    chunks = []
    current_heading = ""
    current_item: list[str] = []

    def flush():
        if current_item:
            item_text = "\n".join(current_item).strip()
            chunks.append(
                {
                    "content": item_text,
                    "section": current_heading,
                    "intent": _infer_intent(item_text),
                    "keywords": extract_keywords(item_text),
                }
            )

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^(第[一二三四五六七八九十0-9]+[章节篇]|[A-Z][A-Z0-9 _-]{2,})$", stripped):
            current_heading = stripped
            continue
        if re.match(r"^(\d+[.)、]|[一二三四五六七八九十]+、)", stripped):
            flush()
            current_item = [f"{current_heading}\n{stripped}".strip()] if current_heading else [stripped]
        elif current_item:
            current_item.append(stripped)
    flush()
    return chunks


def _split_plain_chunks(text: str, chunk_size: int = 500, overlap: int = 80) -> list[dict]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    chunks = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + chunk_size)
        content = normalized[start:end].strip()
        chunks.append(
            {
                "content": content,
                "section": "",
                "intent": _infer_intent(content),
                "keywords": extract_keywords(content),
            }
        )
        if end >= len(normalized):
            break
        start = max(0, end - overlap)
    return chunks


def extract_keywords(text: str) -> list[str]:
    try:
        import jieba

        tokens = [token.strip().lower() for token in jieba.cut_for_search(text) if token.strip()]
    except Exception:
        tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}", text.lower())
    deduped = []
    seen = set()
    for token in tokens:
        if len(token) <= 1:
            continue
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped[:20]


def _is_question_line(line: str) -> bool:
    return (
        line.startswith("Q")
        or line.startswith("问")
        or line.endswith("?")
        or line.endswith("？")
    )


def _infer_intent(text: str) -> str:
    if any(keyword in text for keyword in ["故障", "报错", "错误码", "不工作", "卡住"]):
        return "troubleshooting"
    if any(keyword in text for keyword in ["清洁", "清洗", "保养", "维护"]):
        return "maintenance"
    if any(keyword in text for keyword in ["修复", "维修", "裂缝", "开裂", "烫痕", "白印", "划痕"]):
        return "repair"
    if any(keyword in text for keyword in ["选购", "推荐", "怎么买", "预算"]):
        return "buying_guide"
    return "general"
