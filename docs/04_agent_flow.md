# 04 · Agent 流程设计

> 框架：LangGraph 1.x（StateGraph）｜ 架构：单 Agent + 多工具

## 1. 设计目标

- 用户自然语言提问 → 自动完成「取数 → 分析 → 检索 → 诊断 → 报告」全链路
- 链路可观测（Trace）、可扩展（节点可插拔）、可降级（Mock 模式）

## 2. Agent State（agent/state.py）

```python
class AgentState(TypedDict, total=False):
    # 对话历史（add_messages reducer 自动累积）
    messages: Annotated[list[AnyMessage], add_messages]
    user_question: str     # 原始用户问题
    query_result: dict     # 数据查询结果（get_sales_data / get_campaign_data 输出）
    analysis_result: dict  # 分析结果（Pandas 归因）
    retrieval_docs: list   # 知识检索结果（Document 列表）
    final_report: str      # 最终经营诊断报告
```

> State 是节点间唯一的通信渠道：任何节点返回的 dict 键会合并进 State；`messages` 用 `add_messages` reducer 做消息追加（历史对话 + 工具调用消息自动累积）。

## 3. 图结构（agent/graph.py）

```
                ┌──────────────────────────────────────────┐
                │                                          │
                ▼                                          │(有 tool_calls → 回环)
   START ──▶ intent ──(route_after_intent)─────────────▶ tools(ToolNode)
               │                                         │
               │(无 tool_calls)                          │
               ▼                                         │
            analysis ──▶ rag ──▶ report ──▶ END ◀────────┘
```

| 节点 | 职责 | 实现要点 |
|---|---|---|
| **intent** | 意图分析 | `llm.bind_tools([5 个工具])` 生成 AI 消息；从问题中解析 `store_id / days / start_date` 等参数注入 system prompt；当 LLM 请求工具时产出 tool_calls |
| **tools** | 工具执行 | `ToolNode(ALL_TOOLS)`，执行后结果作为 ToolMessage 回写 messages |
| **analysis** | 数据分析 | 确定性调用 `analysis_business_data`（Pandas），不经过 LLM，保证指标计算准确 |
| **rag** | 知识检索 | 以问题 + 分析结论为 query 调用 `search_operation_knowledge`，检索运营知识库 |
| **report** | 报告生成 | LLM 综合 query_result + analysis_result + retrieval_docs，生成结构化 markdown 经营诊断报告 |

**条件边 `route_after_intent`**：

```python
def route_after_intent(state: AgentState) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "analysis"
```

- LLM 请求工具 → 进 tools 执行，执行完**回环到 intent**（ReAct 循环，直到 LLM 认为数据齐备）
- LLM 不再请求工具 → 进入确定性分析链路 analysis → rag → report

## 4. 节点函数签名（agent/nodes.py）

```python
def intent_node(state: AgentState) -> dict:
    """LLM 意图分析 + 工具调用决策；同时输出 query_result 摘要（供 trace）"""

def tools_node(state: AgentState) -> dict:
    """执行 LLM 请求的工具；由 ToolNode 提供，nodes.py 中无需手写"""

def analysis_node(state: AgentState) -> dict:
    """调用 analysis_business_data(state["query_result"]) → analysis_result"""

def rag_node(state: AgentState) -> dict:
    """调用 search_operation_knowledge(主题) → retrieval_docs"""

def report_node(state: AgentState) -> dict:
    """LLM 综合生成报告 → final_report"""
```

## 5. 工具与节点的两条调用路径（不冲突）

| 路径 | 说明 |
|---|---|
| **LLM 自主调度** | `analysis_business_data` / `search_operation_knowledge` 同时注册为 @tool，LLM 可在 ReAct 循环中按需提前调用（例如先看数据再决定检索什么） |
| **节点确定性直调** | analysis / rag 节点对同一函数做确定性调用，保证「数据分析 → 知识检索 → 报告」主线不依赖 LLM 的随机性 |

两条路径共享同一实现，互不冲突，后续演进为多 Agent 时无需改动工具层。

## 6. System Prompt 设计（intent / report 节点）

```python
INTENT_SYSTEM_PROMPT = """你是连锁门店经营分析助手。根据用户问题决定需要查询哪些数据。
可用工具：get_sales_data(营业额/订单/客单价)、get_campaign_data(推广消耗/ROI)、
analysis_business_data(指标计算)、search_operation_knowledge(运营知识库)、
update_campaign_budget(推广预算调整，需用户授权)。
注意：1) 优先调用数据查询工具获取真实数据；2) 从问题中提取 store_id、时间范围；
3) 数据齐备后停止调用工具。"""
```

```python
REPORT_SYSTEM_PROMPT = """你是资深连锁门店运营顾问。基于【数据概览】【分析结果】【知识库建议】三部分
生成经营诊断报告，必须包含：一、结论摘要；二、核心指标变化；三、异常原因归因（量化到贡献度）；
四、基于知识库的优化建议；五、风险提示。使用 markdown 格式。"""
```

## 7. Mock 模式下的链路（无 Key 可演示）

- `intent_node`：MockLLM 返回无 tool_calls 的 AI 消息；同时若 `BIZ_DATA_MODE=mock`，预置一份**模拟销售数据**到 `query_result`
- `analysis_node`：对模拟数据执行**真实 Pandas 计算**（环比/转化率/品类贡献），输出真实数值
- `rag_node`：向量库为空时返回**内置示例知识片段**（标注来源）
- `report_node`：MockLLM 基于输入生成**带明确占位标注的 markdown 报告**
- 效果：无任何外部依赖即可看到「意图 → 查询(模拟) → 分析(真实计算) → 检索(示例) → 报告」全链路

## 8. 演进：第二阶段 Supervisor 多 Agent

```text
Supervisor(调度)
 ├── 数据分析 Agent（取数 + Pandas 计算）
 ├── 知识检索 Agent（RAG + 引用溯源）
 ├── 报告生成 Agent（归因 + 建议）
 └── 执行 Agent（Playwright，需授权）
```

> 当前单 Agent 的节点边界（intent/tools/analysis/rag/report）即未来各子 Agent 的天然划分，演进成本低。
