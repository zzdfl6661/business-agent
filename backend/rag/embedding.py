"""
Embedding 工厂
==============
DeepSeek 不提供 Embedding API，故向量化独立选型，通过 BIZ_EMBEDDING_PROVIDER 切换：

- chroma_default（默认）: Chroma 内置 ONNX all-MiniLM-L6-v2，零配置即用（开发/骨架）
- fastembed_bge_zh      : BAAI/bge-small-zh-v1.5（384 维），中文检索质量更好
- openai                : text-embedding-3-small（生产，需 Key）

统一返回 LangChain Embeddings 接口，业务层无感。
"""
from __future__ import annotations

import logging

from langchain_core.embeddings import Embeddings

from config.settings import settings

logger = logging.getLogger(__name__)


class ChromaDefaultEmbeddings(Embeddings):
    """将 chromadb 内置 ONNX 嵌入适配为 LangChain Embeddings 接口。"""

    def __init__(self) -> None:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        self._fn = DefaultEmbeddingFunction()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._fn(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._fn([text])[0]


def create_embeddings() -> Embeddings:
    provider = settings.embedding_provider

    if provider == "openai" and settings.openai_api_key:
        from langchain_openai import OpenAIEmbeddings

        logger.info("Embedding provider=openai")
        return OpenAIEmbeddings(model="text-embedding-3-small", api_key=settings.openai_api_key)

    if provider == "fastembed_bge_zh":
        try:
            from langchain_community.embeddings import FastEmbedEmbeddings

            logger.info("Embedding provider=fastembed(bge-small-zh-v1.5)")
            return FastEmbedEmbeddings(model_name="BAAI/bge-small-zh-v1.5")
        except Exception as exc:  # noqa: BLE001
            logger.warning("fastembed 不可用(%s)，回退 chroma_default", exc)

    logger.info("Embedding provider=chroma_default(all-MiniLM-L6-v2)")
    return ChromaDefaultEmbeddings()
