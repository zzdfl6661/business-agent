# 01 · 系统架构说明

> 企业经营智能决策与自动化执行 Agent 系统（Enterprise Intelligent Decision & Automation Agent）
> 版本：v0.1（Phase 0 骨架）｜ 文档状态：设计稿

---

## 1. 系统定位

面向连锁门店运营团队，将「数据获取 → 数据分析 → 问题诊断 → 策略生成 → 自动执行」的人工流程，升级为可由自然语言驱动的智能运营闭环。

**典型用户诉求**：

> "分析最近 7 天 1 号门店营业额下降原因，并给出优化建议。"

**系统自动完成**：理解意图 → 判断所需数据 → 调用数据查询工具 → Pandas 指标计算 → 检索运营知识库 → 异常归因 → 生成经营诊断报告 →（授权后）自动执行运营操作。

## 2. 总体架构

```
┌──────────┐  自然语言提问    ┌───────────────────────┐
│   用户    │ ───────────────▶ │   FastAPI 服务层        │
│          │ ◀─────────────── │  /api/chat  /api/rag    │
└──────────┘   诊断报告        └──────────┬────────────┘
                                         │ graph.invoke
                                ┌────────▼────────┐
                                │  LangGraph Agent │   单 Agent + 多工具
                                │  (StateGraph)    │
                                └────────┬────────┘
                                         │ 节点调度
        ┌────────────────────────────────┼───────────────────────────────┐
        │              │                 │                │               │
┌───────▼──────┐ ┌─────▼──────┐ ┌────────▼──────┐ ┌──────▼──────┐ ┌──────▼──────────┐
│ 数据查询工具   │ │ 推广数据工具 │ │ 数据分析工具   │ │ RAG检索工具   │ │ 自动化执行工具    │
│ get_sales_   │ │ get_camp-  │ │ analysis_     │ │ search_     │ │ update_camp-    │
│ data         │ │ aign_data  │ │ business_data │ │ operation_   │ │ aign_budget     │
│              │ │            │ │ (Pandas)      │ │ knowledge    │ │ (Playwright)    │
└──────┬───────┘ └─────┬──────┘ └────────┬──────┘ └──────┬──────┘ └──────┬──────────┘
       │               │                 │               │               │
┌──────▼───────────────▼─────────────────┐   ┌───────────▼───────┐   ┌────▼─────────────┐
│  MySQL（业务数据）                       │   │ Chroma/Milvus     │   │ 运营后台(模拟页)   │
│  stores/orders/products/campaigns      │   │ 运营知识库向量库    │   │ 阶段二接入        │
└────────────────────────────────────────┘   └───────────────────┘   └──────────────────┘
```

## 3. 分层职责

| 层 | 模块 | 职责 |
|---|---|---|
| 接入层 | `api/chat.py` | HTTP 接口、请求校验、调用图执行、返回报告与执行轨迹 |
| 编排层 | `agent/graph.py` `agent/nodes.py` | LangGraph 状态机编排：意图分析 → 工具调用 → 分析 → 检索 → 报告 |
| 能力层 | `tools/*` | 5 个可被 LLM 调用的工具（数据查询/推广查询/数据分析/RAG/执行） |
| 数据层 | `database/` `rag/` | SQLAlchemy ORM + MySQL；向量库抽象（Chroma/Milvus 可切换） |
| 配置层 | `config/` | pydantic-settings 统一配置、LLM 多 Provider 工厂 |

## 4. 核心设计原则

1. **单 Agent + 多工具**（第一阶段）：不引入复杂多 Agent 编排，用一个 LangGraph 状态机 + 5 个工具覆盖完整链路，降低调试成本，第二阶段再演进 Supervisor/多 Agent。
2. **LLM 不直接碰数据与浏览器**：一切数据访问与外部操作都经工具层——浏览器操作必须走 `Agent → Tool → Playwright`，杜绝 LLM 直操。
3. **确定性计算与生成式推断分离**：指标计算（环比、转化率、ROI）由 Pandas 确定性完成；归因解释、报告润色由 LLM 完成，避免"幻觉算数"。
4. **仅真实链路（无 Mock 红线）**：无任何 MockLLM / 模拟数据 / 内置示例知识——未配置 LLM Key 时 `create_llm` 抛出明确 `ValueError`；数据库不可用时数据工具返回明确错误。新增功能必须走真实数据链路，杜绝"假报告"误导。
5. **抽象可替换**：向量库（Chroma/Milvus）、Embedding、LLM Provider 均通过工厂/抽象类解耦，环境变量切换。

## 5. 请求生命周期（一次完整的经营诊断）

```
POST /api/chat {question: "分析最近7天1号店营业额下降原因"}
   │
   ▼
intent 节点：LLM 理解意图 → 决定调用 get_sales_data(store_id=1, days=7) / get_campaign_data(...)
   │
   ▼
tools 节点：ToolNode 执行查询 → 结果写入 state.query_result
   │（LLM 无更多工具请求）
   ▼
analysis 节点：Pandas 计算 GMV 环比、转化率、客单价、品类贡献 → state.analysis_result
   │
   ▼
rag 节点：以诊断主题检索运营知识库（SOP/推广策略/异常案例）→ state.retrieval_docs
   │
   ▼
report 节点：LLM 综合三者生成 markdown 经营诊断报告 → state.final_report
   │
   ▼
返回 {report, trace}（trace 为各节点中间结果，供前端展示"思考轨迹"）
```

## 6. 执行轨迹（Trace）

`/api/chat` 响应中附带 `trace` 字段，记录每个节点的输入输出摘要：

```json
{
  "report": "## 经营诊断报告 ...",
  "trace": [
    {"node": "intent",   "detail": "识别需求：营业额下降归因，参数 {store_id: 1, days: 7}"},
    {"node": "tools",    "detail": "get_sales_data → 营业额 128.5万，环比 -12.3%"},
    {"node": "analysis", "detail": "归因因子：品类B销量下滑贡献 -62%，周末客流 -21%"},
    {"node": "rag",      "detail": "命中 3 篇知识：推广优化策略.md 等"},
    {"node": "report",   "detail": "报告生成完成（1523 字）"}
  ]
}
```

## 7. 演进路线（Phase 2+）

| 演进项 | 说明 |
|---|---|
| 自动化执行 | `update_campaign_budget` 接入 Playwright 真实操作，`confirm=True` 才执行（人工授权） |
| 多 Agent | Supervisor 调度：数据分析 Agent / 知识检索 Agent / 执行 Agent 分工 |
| 向量库 | Milvus（docker-compose 启动），抽象层已预留 |
| 部署 | Dockerfile + docker-compose（app + mysql + chroma volume） |
| 运营闭环 | 自动执行后回读数据验证效果，形成「执行 → 复检」循环 |
