from abc import ABC, abstractmethod
from langchain_community.chat_models import ChatTongyi
from langchain_core.language_models import BaseChatModel
from langchain_core.embeddings import Embeddings
from utils.config_handler import rag_conf
from langchain_community.embeddings import DashScopeEmbeddings

class BaseModelFactory(ABC):
    @abstractmethod
    def generate(self) -> Embeddings | BaseChatModel | None:
        pass

class ChatModelFactory(BaseModelFactory):
    def generate(self) -> Embeddings | BaseChatModel | None:
        return ChatTongyi(model=rag_conf["chat_model_name"])

class EmbeddingsFactory(BaseModelFactory):
    def generate(self) -> Embeddings | BaseChatModel | None:
        return DashScopeEmbeddings(model=rag_conf["embedding_model_name"])

class MemoryModelFactory(BaseModelFactory):
    def generate(self) -> Embeddings | BaseChatModel | None:
        return ChatTongyi(model=rag_conf.get("memory_model_name", "qwen-mt-flash"))


chat_model = ChatModelFactory().generate()
memory_model = MemoryModelFactory().generate()
embed_model = EmbeddingsFactory().generate()
# /runtime.context
