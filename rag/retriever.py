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

import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import date, timedelta

from config.settings import settings
from rag.embedding import create_embeddings

logger = logging.getLogger(__name__)

# 相似度阈值（bge-small-zh 中文检索实测 0.5~0.6 区间波动；0.6 会过度过滤漏召回）
SCORE_THRESHOLD = 0.5
EXPERIENCE_MAX_AGE_DAYS = 30  # 经验层（历史报告）有效期：超期过滤
EXPERIENCE_DECAY_MIN = 0.3    # 时间衰减下限

# BM25 语料缓存：避免每次查询全量拉取整个 collection（O(N) → 命中缓存 O(1)）
BM25_CACHE_TTL = 300          # 秒；ingest/upload 后经 invalidate_cache() 立即失效


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
              max_age_days: int | None = None, filter_meta: dict | None = None) -> list[dict]:
        """相似度检索，返回 [{content, metadata, score, effective_score, age_days}]。

        - max_age_days：对带 report_date 元数据的文档做时间过滤 + 时间衰减
          （无 report_date 的静态知识不受影响）
        - filter_meta：额外标量过滤（如 store_id），与 filter_type 按 $and 组合（经验层按门店隔离）
        - effective_score = score × 时间衰减系数（越旧越低，防过期经验误导）
        """

    @abstractmethod
    def delete(self, doc_type: str | None = None, **extra_where) -> None:
        """按 doc_type + 额外条件清空（None 表示全量）。"""

    @abstractmethod
    def count(self) -> int:
        """当前文档块数量。"""

    def invalidate_cache(self) -> None:
        """使内部缓存失效（ingest/upload 后调用；默认空实现）。"""


class ChromaClient(VectorStoreClient):
    """Chroma 持久化实现。

    存储结构（父子切割去冗余，#9）：
    - 主 collection（operation_knowledge）：子块向量（metadata 只带 parent_id 引用，不再冗余 parent_content）
    - 父块 collection（parent_docs）：按 parent_id 存父块全文，命中子块后回查
    兼容旧数据：metadata 中残留 parent_content 的旧 chunk 仍可正常回查。
    """

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
        # 父块独立 collection：仅按 id 回查全文，不参与向量检索
        self._parent_col = self._client.get_or_create_collection(
            "parent_docs", metadata={"hnsw:space": "cosine"}
        )
        self._bm25_cache: dict[str, dict] = {}
        logger.info("Chroma 就绪：dir=%s（父块独立存储）", settings.chroma_dir)

    # ------------------------------------------------------------ 入库
    def add_documents(self, docs, doc_type: str | None = None) -> None:
        """入库：子块进主库（去 parent_content 冗余），父块抽到 parent_docs。"""
        plain: list = []
        for doc in docs:
            meta = dict(doc.metadata)
            pid = meta.get("parent_id")
            parent_content = meta.pop("parent_content", None)
            if pid and parent_content:
                # 父块全文 → 独立 collection（upsert 幂等：重复 ingest 不累积）
                self._parent_col.upsert(ids=[str(pid)], documents=[parent_content])
                # 子块只保留 parent_id 引用
                doc.metadata = meta
            plain.append(doc)
        if plain:
            self._store.add_documents(plain)
        self.invalidate_cache()

    # ------------------------------------------------------------ 检索
    def query(self, query: str, top_k: int = 5, filter_type: str | None = None,
              max_age_days: int | None = None, hybrid: bool = True,
              exclude_types: list[str] | None = None,
              filter_meta: dict | None = None) -> list[dict]:
        """相似度检索 + 可选时间过滤/衰减 + 混合检索（向量 × BM25 RRF 融合）。

        注意：Chroma 的 $gte 仅支持数值比较 → report_date 存整数 YYYYMMDD。
        hybrid=True（默认）：向量候选 + jieba 分词 BM25 关键词检索，RRF 融合重排。
        中文场景（英文 embedding 模型）下关键词召回显著提升专有名词/话术类查询。
        exclude_types：排除指定 doc_type（如知识层检索排除 report 经验层，避免历史报告污染）。
        filter_meta：额外标量过滤（如经验层按 store_id 隔离），与 filter_type $and 组合。
        """
        extra: dict = dict(filter_meta or {})
        if max_age_days:
            cutoff = int((date.today() - timedelta(days=max_age_days)).strftime("%Y%m%d"))
            extra["report_date"] = {"$gte": cutoff}
        filter_ = _build_where(filter_type, **extra)
        if exclude_types:
            ne_conds = [{"doc_type": {"$ne": t}} for t in exclude_types]
            filter_ = (
                {"$and": [filter_, *ne_conds]}
                if filter_ else (ne_conds[0] if len(ne_conds) == 1 else {"$and": ne_conds})
            )

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

    def _expand_parent(self, result: dict) -> None:
        """命中子块 → 回查父块全文替换返回内容（优先旧数据 parent_content，其次 parent_docs 库）。"""
        meta = result.get("metadata") or {}
        parent = meta.get("parent_content")  # 兼容旧结构（无冗余存储前的数据）
        if parent:
            result["content"] = parent
            result["is_child"] = True
            return
        pid = meta.get("parent_id")
        if pid:
            try:
                got = self._parent_col.get(ids=[str(pid)], include=["documents"])
                docs = got.get("documents") or []
                if docs and docs[0]:
                    result["content"] = docs[0]
                    result["is_child"] = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("父块回查失败（parent_id=%s）：%s", pid, exc)

    # ------------------------------------------------------------ BM25（进程级缓存）
    def _bm25_corpus(self, filter_: dict | None) -> tuple[list[str], list[dict], "BM25Okapi | None"]:
        """获取 (texts, metas, bm25)。语料按过滤条件缓存（ingest/upload 后 invalidate 失效）。"""
        key = json.dumps(filter_, sort_keys=True, ensure_ascii=False, default=str)
        now = time.time()
        cached = self._bm25_cache.get(key)
        if cached and now - cached["ts"] < BM25_CACHE_TTL:
            return cached["texts"], cached["metas"], cached["bm25"]

        try:
            import jieba
            from rank_bm25 import BM25Okapi
        except ImportError:
            return [], [], None

        try:
            col = self._store._collection
            got = col.get(where=filter_ or None, include=["documents", "metadatas"])
            all_texts = got["documents"] or []
            all_metas = got["metadatas"] or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("BM25 语料获取失败：%s（降级纯向量）", exc)
            return [], [], None
        if not all_texts:
            return [], [], None

        def _tok(t: str) -> list[str]:
            return [w for w in jieba.cut(t) if w.strip() and len(w.strip()) > 1]

        corpus = [_tok(t) for t in all_texts]
        bm25 = BM25Okapi(corpus) if any(corpus) else None
        self._bm25_cache[key] = {"ts": now, "texts": all_texts, "metas": all_metas, "bm25": bm25}
        logger.info("BM25 语料缓存重建：filter=%s（%s 条）", key[:60], len(all_texts))
        return all_texts, all_metas, bm25

    def invalidate_cache(self) -> None:
        """ingest/upload 后失效 BM25 语料缓存（避免下次查询仍用旧语料）。"""
        self._bm25_cache.clear()

    def _hybrid_rerank(self, query: str, vec_results: list[dict], filter_: dict | None,
                       top_k: int) -> list[dict]:
        """向量结果 + BM25 关键词结果 RRF 融合。

        - BM25 语料：同过滤条件下的全量文档（jieba 分词），进程级缓存（#9）
        - RRF: score = Σ 1/(k + rank_i)，k=60
        - BM25 独有命中的条目保留（标记 bm25_only），弥补向量阈值过滤的漏召回
        """
        all_texts, all_metas, bm25 = self._bm25_corpus(filter_)
        if bm25 is None or not all_texts:
            return vec_results

        import jieba  # query 分词（语料已在 _bm25_corpus 内分词）

        def _tok(t: str) -> list[str]:
            return [w for w in jieba.cut(t) if w.strip() and len(w.strip()) > 1]

        q_tok = _tok(query)
        if not q_tok:
            return vec_results

        scores = bm25.get_scores(q_tok)
        bm25_order = [i for i in sorted(range(len(all_texts)), key=lambda i: -scores[i]) if scores[i] > 0]

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
        self.invalidate_cache()

    def count(self) -> int:
        return self._store._collection.count()


class MilvusClient(VectorStoreClient):
    """Milvus 实现（阶段二：docker-compose 起 Milvus 后落地）。"""

    def __init__(self) -> None:
        raise NotImplementedError("Milvus 支持属第二阶段，请使用 BIZ_VECTOR_STORE_TYPE=chroma")

    def add_documents(self, docs, doc_type: str | None = None) -> None:
        raise NotImplementedError

    def query(self, query: str, top_k: int = 5, filter_type: str | None = None,
              max_age_days: int | None = None, filter_meta: dict | None = None) -> list[dict]:
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
