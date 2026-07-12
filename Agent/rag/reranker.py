import os
from pathlib import Path
from typing import List

from langchain_core.documents import Document

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


class BGEReranker:
    def __init__(self):
        from transformers import utils as transformers_utils
        from transformers.utils import import_utils

        # Text reranking does not need torchvision. Disabling the optional image
        # backend also avoids torch/torchvision version conflicts on local hosts.
        import_utils.is_torchvision_available = lambda: False
        transformers_utils.is_torchvision_available = lambda: False

        from sentence_transformers import CrossEncoder

        default_model_path = Path(__file__).resolve().parents[1] / "models" / "bge-reranker-base"
        model_path = os.getenv("BGE_MODEL_PATH", str(default_model_path))
        local_only = not os.getenv("BGE_ALLOW_DOWNLOAD", "")
        self.model = CrossEncoder(model_path, local_files_only=local_only)

    def rerank(self, query: str, docs: List[Document], top_k: int = 3):
        if not docs:
            return []
        try:
            scores = self.score_pairs(query, [doc.page_content for doc in docs])
            ranked = sorted(zip(docs, scores), key=lambda item: item[1], reverse=True)
            return [doc for doc, _ in ranked[:top_k]]
        except Exception:
            return docs[:top_k]

    def score_pairs(self, query: str, passages: List[str]) -> List[float]:
        if not passages:
            return []
        pairs = [(query, passage) for passage in passages]
        return list(self.model.predict(pairs))
