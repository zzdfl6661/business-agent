# 05 · RAG 知识库设计

> 运营知识库：门店 SOP / 推广策略 / 活动规则 / 历史诊断案例
> 流水线：Document Loader → 文本切分 → Embedding → Vector DB → Retriever → LLM

## 1. 整体流水线

```
┌─────────────┐   ┌──────────────┐   ┌───────────┐   ┌───────────┐   ┌──────────┐   ┌─────┐
│ 文档上传     │→│ 解析+切分      │→│ Embedding │→│ 向量库     │→│ Retriever │→│ LLM │
│ (md/pdf/txt)│  │ RecursiveText │  │ 工厂       │  │ Chroma/   │  │ top_k+阈值│  │ 生成│
└─────────────┘   └──────────────┘   └───────────┘   │ Milvus    │   └──────────┘   └─────┘
                                                       └───────────┘
```

## 2. 示例知识文档（backend/rag/data/）

| 文档 | 内容 | 用途 |
|---|---|---|
| 门店运营SOP.md | 开店/关店流程、商品陈列、员工排班、日报检查项 | 常规运营规则问答 |
| 推广优化策略.md | 预算分配原则、ROI 分层调整、渠道选择、出价策略 | 推广优化建议 |
| 活动运营规则.md | 满减/折扣/新客立减活动设计规范、报名与复盘流程 | 活动方案建议 |
| ROI异常分析案例.md | 历史 ROI 异常诊断案例（预算花超/素材衰减/品类问题） | 归因参考（few-shot 式） |

> 启动时自动加载入库；另提供 `POST /api/rag/upload` 支持增量上传新文档。

## 3. 文本切分策略（rag/loader.py）

```python
RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=80,
    separators=["\n\n", "\n", "。", "！", "？", " "],  # 中文优先按段落/句子切
)
```

- 按字符粒度（中文场景不按 token），overlap 80 保留段落上下文
- 每个 chunk 记录 metadata：`doc_type`（sop/strategy/activity/case）、`source`（文件名）

## 4. Embedding 选型（rag/embedding.py）

> ⚠️ **DeepSeek 不提供 Embedding API**，需独立方案：

| Provider | 模型 | 适用阶段 | 说明 |
|---|---|---|---|
| `chroma_default`（默认） | all-MiniLM-L6-v2（ONNX） | 开发/骨架 | Chroma 内置，零配置即用；英文效果好，中文一般 |
| `fastembed_bge_zh` | BAAI/bge-small-zh-v1.5（384 维） | 开发（中文） | fastembed 本地推理，中文检索质量明显提升 |
| `openai` | text-embedding-3-small | 生产 | 需 OpenAI Key，质量最好 |
| `milvus` | 随部署选型 | 生产 | 抽象层对接 |

`EmbeddingFactory` 统一入口，`BIZ_EMBEDDING_PROVIDER` 切换，业务层无感。

## 5. 向量库抽象（rag/retriever.py）

```python
class VectorStoreClient(ABC):
    def add_documents(self, docs, doc_type) -> None: ...
    def query(self, query: str, top_k: int, filter_type=None) -> list[dict]: ...
    def delete(self, doc_type: str) -> None: ...

class ChromaClient(VectorStoreClient): ...   # PersistentClient + langchain-chroma
class MilvusClient(VectorStoreClient): ...   # 阶段二实现
```

- `BIZ_VECTOR_STORE_TYPE=chroma/milvus` 切换，业务层只依赖抽象
- **Chroma**：`PersistentClient(path=./chroma_db)`，collection=`operation_knowledge`，`metadata={"hnsw:space":"cosine"}`
- **检索**：`similarity_search_with_score(query, k=5)` → cosine 阈值 **0.6** 过滤低相关，按 `doc_type` 可过滤
- 返回结构：`[{content, metadata:{doc_type, source}, score}]`

## 6. 工具层接入（tools/rag_tool.py）

```python
@tool
def search_operation_knowledge(query: str, top_k: int = 5) -> list[dict]:
    """检索运营知识库（门店SOP/推广策略/活动规则/历史诊断案例），返回知识片段列表。
    参数：query 检索主题；top_k 返回条数。"""
```

- 供 LLM 在 ReAct 循环中自主调用，也由 rag 节点确定性调用
- 向量库为空/不可用时，降级返回内置示例知识片段（标注 `"fallback": true`）

## 7. 索引构建流程（启动 / 上传）

```python
def ingest_documents(paths: list[str]) -> int:
    docs = load(paths)                 # TextLoader / PyPDFLoader
    chunks = split(docs)               # RecursiveCharacterTextSplitter
    client.add_documents(chunks)       # Embedding + 入库（幂等：先 delete 同 doc_type）
    return len(chunks)
```

## 8. 检索质量优化方向（演进）

- 中文场景默认切 `bge-small-zh-v1.5`（零成本提升明显）
- 引入 rerank（如 bge-reranker）对 top_k 结果重排
- 结果带 `score` 与 `source`，报告节点强制引用来源，防幻觉
- Milvus 支持标量过滤与分区，可按 `doc_type` 分 partition

## 9. 向量库内容规划（建议，分层）

当前只存了**静态运营知识**。对企业经营 Agent，建议按"三层"规划（doc_type 已支持分层过滤）：

| 层 | doc_type | 内容 | 状态 |
|---|---|---|---|
| **知识层**（静态） | sop / strategy / activity / case | 门店运营SOP、推广策略、活动规则、诊断案例 | ✅ 已实现 |
| **经验层**（动态） | report | **历史诊断报告自动入库**：每次 Agent 生成的经营报告写入向量库 | ⬜ 推荐实现 |
| **操作层**（执行） | ops | 后台操作手册：改预算/建活动的路径与按钮（供 Playwright 执行参考） | ⬜ 阶段二 |

**经验层（历史报告）价值最大**：Agent 每次诊断后自动把报告入库，后续遇到相似问题可直接引用历史结论——形成"自我进化"，报告口径更一致、少走弯路。实现要点：
- 报告生成节点完成后，把 `final_report` 切分入库（doc_type=report，metadata 存 门店/时间段/异常类型）
- 检索时对 `report` 类型可降权重或单独过滤（防"经验过期"：metadata 带时间，检索按时间衰减）

**操作层**配合阶段二：Playwright 执行前先检索"操作手册"获取页面路径/选择器（操作型 RAG），让自动化流程可配置而非硬编码。
