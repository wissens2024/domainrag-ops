"""Protocol/Adapter 인터페이스 (ADR-002).

LLMClient · Embedder · Retriever · Reranker · VectorStore · DocumentParser ·
Chunker · AuthAdapter · KeyHubAdapter · PIIDetector — 모두 Protocol.
구현체 교체는 configs 변경.
"""

from .auth import AuthAdapter, UserContext
from .chunker import Chunker
from .embedder import Embedder
from .keyhub import KeyHubAdapter
from .llm_client import LLMClient
from .parser import DocumentParser
from .pii import PIIDetector
from .reranker import Reranker
from .retriever import Retriever
from .vector_store import VectorStore

__all__ = [
    "AuthAdapter",
    "UserContext",
    "Chunker",
    "Embedder",
    "KeyHubAdapter",
    "LLMClient",
    "DocumentParser",
    "PIIDetector",
    "Reranker",
    "Retriever",
    "VectorStore",
]
