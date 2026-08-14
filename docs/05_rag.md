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

## 2. 内置知识文档（rag/data/，启动自动 ingest，99 chunks）

| 文档 | 内容 | 用途 |
|---|---|---|
| 公司背景.docx | 公司简介、门店规模/分布、业务板块 | 公司介绍类问答 |
| 公司高管核心人员名单.docx | 高管名单与分工 | 组织架构问答 |
| 门店日常工作安排.docx | 店长/店员日常任务、值班排班、卫生巡检 | 日常工作问答 |
| 员工手册.docx | 考勤、试用期、纪律 | 制度问答 |
| 门店晋升制度.docx | 晋升条件、报名、评估流程 | 晋升问答 |
| 门店薪资绩效管理办法.docx | 薪资构成、绩效奖金规则 | 薪资绩效问答 |
| 满意度回访话术.pdf | 顾客回访/投诉安抚话术 | 话术参考 |

> 启动时自动加载入库；另提供 `POST /api/rag/upload` 支持增量上传新文档
> （basename 白名单 + 20MB + 按文件名幂等，不误删其他文档）。

## 3. 文本切分策略（rag/loader.py，父子切割）

```python
CHILD_CHUNK_SIZE = 350     # 子块：语义聚焦，供 embedding/BM25 精准检索
CHILD_CHUNK_OVERLAP = 50
PARENT_CHUNK_SIZE = 1200   # 父块：按章节标题切（第X章/一、/1.2），上下文完整供 LLM
PARENT_CHUNK_OVERLAP = 150
PARENT_MAX_CHARS = 2500    # 超长章节按次级标题/字符细分
```

- 子块 metadata 携带 `parent_id` 引用；**父块全文存独立 `parent_docs` collection**（#9 去冗余），命中子块后回查父块返回
- 每个 chunk 记录 metadata：`doc_type`、`source`（文件名）、`parent_id`

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
    def query(self, query, top_k, filter_type=None, max_age_days=None, filter_meta=None) -> list[dict]: ...
    def delete(self, doc_type, **extra_where) -> None: ...
    def count(self) -> int: ...
    def invalidate_cache(self) -> None: ...   # BM25 语料缓存失效

class ChromaClient(VectorStoreClient): ...   # PersistentClient + langchain-chroma + parent_docs 父块库
class MilvusClient(VectorStoreClient): ...   # 阶段二实现
```

- `BIZ_VECTOR_STORE_TYPE=chroma/milvus` 切换，业务层只依赖抽象
- **Chroma**：`PersistentClient(path=./chroma_db)`，主 collection=`operation_knowledge`，父块 collection=`parent_docs`，`metadata={"hnsw:space":"cosine"}`
- **检索**：`similarity_search_with_score(query, k=top_k*3)` → cosine 阈值 **0.5** 过滤（bge-zh 中文 0.5~0.6 波动，0.6 会过度过滤）→ **向量 + BM25 RRF 融合**（BM25 语料进程级缓存，#9）
- **经验层**：`filter_type="report"` + `max_age_days=30` 时间衰减；`filter_meta={"store_id": ...}` 按门店隔离（#3）
- 返回结构：`[{content, metadata:{doc_type, source, parent_id, report_id...}, score, effective_score, age_days}]`

## 6. 工具层接入（tools/rag_tool.py）

```python
@tool
def search_operation_knowledge(query: str, top_k: int = 5) -> list[dict]:
    """检索运营知识库（门店SOP/推广策略/活动规则/历史诊断案例），返回知识片段列表。
    参数：query 检索主题；top_k 返回条数。"""
```

- 供 LLM 在 ReAct 循环中自主调用，也由 rag 节点确定性调用
- 知识层检索 `exclude_types=["report"]`（经验层由 rag_node 单独查询，避免历史报告污染知识问答）
- 向量库未就绪时返回空列表（**无内置示例知识兜底**，无 Mock 红线）

## 7. 索引构建流程（启动 / 上传）

```python
def ingest(paths: list[str] | None = None) -> int:
    docs = load(paths)                 # TextLoader / PyPDFLoader / DOCXLoader
    chunks = split_documents_hierarchical(docs)   # 父子切割：父块抽 parent_docs，子块进主库
    client.add_documents(chunks)       # Embedding + 入库（幂等：只删本次涉及 doc_type）
    return len(chunks)
```

> 上传走 `loader.upload_and_ingest`：按 source（文件名）幂等清理，不误删其他文档；ingest/upload 后 BM25 缓存自动失效。

## 8. 检索质量评测（#11）与演进

- **评测**：`data/eval/golden_set.json`（21 条 golden set）+ `scripts/eval_rag.py`（Hit@k）；改 embedding/切块参数后必须跑，避免"感觉变好"假象
- 中文场景默认切 `bge-small-zh-v1.5`（零成本提升明显）；可选 `fastembed_bge_m3`（多语言更优，需重建向量库）
- 演进：引入 rerank（如 bge-reranker）对 top_k 结果精排（Query Rewrite + HyDE 已补召回，精排收益最直接）
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
