"""
向量库抽象层
============
VectorStoreClient(ABC) 定义 add/query/delete 契约，业务层只依赖抽象：

- ChromaClient : 开发环境（PersistentClient + langchain-chroma），BIZ_CHROMA_DIR 持久化
- MilvusClient : 生产环境（阶段二实现，契约已固定）

通过 get_vector_client() 按 BIZ_VECTOR_STORE_TYPE 工厂创建。
检索：cosine 相似度 ≥ 0.6 过滤，按 doc_type 可选过滤，返回 [{content, metadata, score}]。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, timedelta

from config.settings import settings
from rag.embedding import create_embeddings

logger = logging.getLogger(__name__)

SCORE_THRESHOLD = 0.5  # 相似度阈值（all-MiniLM 中文相似度偏低，0.6 会过度过滤）
EXPERIENCE_MAX_AGE_DAYS = 30  # 经验层（历史报告）有效期：超期过滤
EXPERIENCE_DECAY_MIN = 0.3    # 时间衰减下限


def _build_where(doc_type: str | None = None, **extra) -> dict | None:
    """构造 Chroma where：单条件直接返回；多条件用 $and 包裹（chroma 顶层只允许一个操作符）。"""
    conds: list[dict] = []
    if doc_type:
        conds.append({"doc_type": doc_type})
    for k, v in extra.items():
        conds.append({k: v})
    if not conds:
        return None
    if len(conds) == 1:
        return conds[0]
    return {"$and": conds}


class VectorStoreClient(ABC):
    """向量库统一契约（Chroma / Milvus 共实现）。"""

    @abstractmethod
    def add_documents(self, docs, doc_type: str | None = None) -> None:
        """批量入库。doc_type 用于标量过滤。"""

    @abstractmethod
    def query(self, query: str, top_k: int = 5, filter_type: str | None = None,
              max_age_days: int | None = None) -> list[dict]:
        """相似度检索，返回 [{content, metadata, score, effective_score, age_days}]。

        - max_age_days：对带 report_date 元数据的文档做时间过滤 + 时间衰减
          （无 report_date 的静态知识不受影响）
        - effective_score = score × 时间衰减系数（越旧越低，防过期经验误导）
        """

    @abstractmethod
    def delete(self, doc_type: str | None = None, **extra_where) -> None:
        """按 doc_type + 额外条件清空（None 表示全量）。"""

    @abstractmethod
    def count(self) -> int:
        """当前文档块数量。"""


class ChromaClient(VectorStoreClient):
    """Chroma 持久化实现。"""

    def __init__(self) -> None:
        from chromadb import PersistentClient
        from langchain_chroma import Chroma

        self._client = PersistentClient(path=settings.chroma_dir)
        self._store = Chroma(
            client=self._client,
            collection_name="operation_knowledge",
            embedding_function=create_embeddings(),
            collection_metadata={"hnsw:space": "cosine"},
        )
        logger.info("Chroma 就绪：dir=%s", settings.chroma_dir)

    def add_documents(self, docs, doc_type: str | None = None) -> None:
        self._store.add_documents(docs)

    def query(self, query: str, top_k: int = 5, filter_type: str | None = None,
              max_age_days: int | None = None, hybrid: bool = True) -> list[dict]:
        """相似度检索 + 可选时间过滤/衰减 + 混合检索（向量 × BM25 RRF 融合）。

        注意：Chroma 的 $gte 仅支持数值比较 → report_date 存整数 YYYYMMDD。
        hybrid=True（默认）：向量候选 + jieba 分词 BM25 关键词检索，RRF 融合重排。
        中文场景（英文 embedding 模型）下关键词召回显著提升专有名词/话术类查询。
        """
        extra: dict = {}
        if max_age_days:
            cutoff = int((date.today() - timedelta(days=max_age_days)).strftime("%Y%m%d"))
            extra["report_date"] = {"$gte": cutoff}
        filter_ = _build_where(filter_type, **extra)

        # 多取候选再按 effective_score 排序截断，保证衰减后仍有足够结果
        hits = self._store.similarity_search_with_score(query, k=max(top_k * 3, 30), filter=filter_)

        results = []
        for doc, score in hits:
            sim = round(1.0 - score, 4)  # cosine 距离 → 相似度
            meta = doc.metadata
            age_days = 0
            rd = meta.get("report_date")
            if rd:
                try:
                    rd_int = int(rd) if not isinstance(rd, int) else rd
                    age_days = (date.today() - date.fromisoformat(f"{rd_int // 10000}-{rd_int % 10000 // 100:02d}-{rd_int % 100:02d}")).days
                except (TypeError, ValueError):
                    age_days = 0
            effective = sim
            if age_days > 0:
                window = max_age_days or EXPERIENCE_MAX_AGE_DAYS
                decay = max(EXPERIENCE_DECAY_MIN, 1.0 - age_days / window)
                effective = round(sim * decay, 4)
            results.append({
                "content": doc.page_content,
                "metadata": meta,
                "score": sim,
                "effective_score": effective,
                "age_days": age_days,
            })

        results.sort(key=lambda r: r["effective_score"], reverse=True)
        results = results[:top_k]
        # 阈值用原始相似度判断（时间衰减只影响排序，不把旧经验过滤掉）
        results = [r for r in results if r["score"] >= SCORE_THRESHOLD]

        # 混合检索：向量 + BM25 RRF 融合（提升中文关键词召回）
        if hybrid:
            results = self._hybrid_rerank(query, results, filter_, top_k)

        # 父子切割回查：命中子块 → 返回父块内容（上下文完整，检索精度由子块保证）
        for r in results:
            self._expand_parent(r)
        # 同一父块去重（多个子块命中同一父块 → 只保留最相关一条，避免重复内容）
        seen_parent: set = set()
        deduped: list[dict] = []
        for r in results:
            pid = (r.get("metadata") or {}).get("parent_id")
            if pid and pid in seen_parent:
                continue
            if pid:
                seen_parent.add(pid)
            deduped.append(r)
        return deduped

    @staticmethod
    def _expand_parent(result: dict) -> None:
        """若命中的是子块（带 parent_content），用父块全文替换返回内容。"""
        meta = result.get("metadata") or {}
        parent = meta.get("parent_content")
        if parent:
            result["content"] = parent
            result["is_child"] = True

    def _hybrid_rerank(self, query: str, vec_results: list[dict], filter_: dict | None,
                       top_k: int) -> list[dict]:
        """向量结果 + BM25 关键词结果 RRF 融合。

        - BM25 语料：同过滤条件下的全量文档（jieba 分词）
        - RRF: score = Σ 1/(k + rank_i)，k=60
        - BM25 独有命中的条目保留（标记 bm25_only），弥补向量阈值过滤的漏召回
        """
        try:
            import jieba
            from rank_bm25 import BM25Okapi
        except ImportError:
            return vec_results

        try:
            col = self._store._collection
            got = col.get(where=filter_ or None, include=["documents", "metadatas"])
            all_texts = got["documents"] or []
            all_metas = got["metadatas"] or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("BM25 语料获取失败：%s（降级纯向量）", exc)
            return vec_results
        if not all_texts:
            return vec_results

        def _tok(t: str) -> list[str]:
            return [w for w in jieba.cut(t) if w.strip() and len(w.strip()) > 1]

        corpus = [_tok(t) for t in all_texts]
        q_tok = _tok(query)
        if not q_tok or not any(corpus):
            return vec_results

        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(q_tok)
        bm25_order = [i for i in sorted(range(len(corpus)), key=lambda i: -scores[i]) if scores[i] > 0]

        # RRF 融合
        K = 60
        vec_rank = {r["content"]: i + 1 for i, r in enumerate(vec_results)}
        bm_rank = {all_texts[i]: i + 1 for i in bm25_order}
        keys = list(vec_rank.keys())
        for c in bm_rank:
            if c not in vec_rank:
                keys.append(c)
        keys.sort(
            key=lambda c: 1.0 / (K + vec_rank.get(c, 10**9)) + 1.0 / (K + bm_rank.get(c, 10**9)),
            reverse=True,
        )

        by_content = {r["content"]: r for r in vec_results}
        meta_by_content = {t: (all_metas[i] or {}) for i, t in enumerate(all_texts)}
        min_score = min((r.get("effective_score", r.get("score", 0)) for r in vec_results), default=0.0)
        merged = []
        for c in keys[:top_k]:
            if c in by_content:
                merged.append(by_content[c])
            else:
                merged.append({
                    "content": c,
                    "metadata": meta_by_content.get(c, {}),  # 补回 metadata（source 溯源）
                    "score": min_score, "effective_score": min_score,
                    "age_days": 0, "bm25_only": True,
                })
        return merged

    def delete(self, doc_type: str | None = None, **extra_where) -> None:
        collection = self._store._collection
        where = _build_where(doc_type, **extra_where)
        if where:
            collection.delete(where=where)
        else:
            # chroma 不接受空 where 字典；按 id 全量删除
            ids = collection.get(include=[])["ids"]
            if ids:
                collection.delete(ids=ids)

    def count(self) -> int:
        return self._store._collection.count()


class MilvusClient(VectorStoreClient):
    """Milvus 实现（阶段二：docker-compose 起 Milvus 后落地）。"""

    def __init__(self) -> None:
        raise NotImplementedError("Milvus 支持属第二阶段，请使用 BIZ_VECTOR_STORE_TYPE=chroma")

    def add_documents(self, docs, doc_type: str | None = None) -> None:
        raise NotImplementedError

    def query(self, query: str, top_k: int = 5, filter_type: str | None = None,
              max_age_days: int | None = None) -> list[dict]:
        raise NotImplementedError

    def delete(self, doc_type: str | None = None, **extra_where) -> None:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError


_client: VectorStoreClient | None = None


def get_vector_client() -> VectorStoreClient:
    """按配置创建向量库客户端（单例）。"""
    global _client
    if _client is None:
        if settings.vector_store_type == "milvus":
            _client = MilvusClient()
        else:
            _client = ChromaClient()
    return _client


def reset_vector_client() -> None:
    """测试/热切换用。"""
    global _client
    _client = None
