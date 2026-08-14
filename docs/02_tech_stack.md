# 02 · 技术选型说明

> 本文件说明各技术组件选型理由、版本约束与关键避坑点。

## 1. 选型总览

| 关注点 | 选型 | 版本约束 | 理由 |
|---|---|---|---|
| 后端框架 | FastAPI | >=0.115 | 异步、自动 OpenAPI 文档、pydantic 原生支持，企业 API 标准选择 |
| Agent 编排 | LangGraph | ==1.2.* | 有向图状态机，天然表达「意图→工具→分析→报告」流水线，可观测、可恢复 |
| LLM 集成 | LangChain | 1.x（core 1.4.*） | 生态最全；工具调用、消息协议统一，1.0 起 API 稳定 |
| 模型 | DeepSeek（默认）/ OpenAI / 本地 | langchain-deepseek 1.1.* | 国内直连、成本低；工厂模式三 Provider 切换 |
| RAG 向量库 | Chroma（开发）/ Milvus（生产） | chromadb >=1.5.0 | 开发零依赖可跑；Milvus 预留抽象层 |
| 业务库 | MySQL | 8.x | 既有业务系统标准，四张核心表 |
| ORM | SQLAlchemy | ==2.0.51 | 2.x 声明式 + 类型注解，企业标准 |
| 数据分析 | Pandas / NumPy | pandas >=2.2.3 | 指标计算、环比、归因聚合的标准工具 |
| 浏览器自动化 | Playwright | 1.61（阶段二） | 微软维护，跨浏览器，适合运营后台模拟操作 |
| 部署 | Docker | — | 阶段二 docker-compose 一键起 |

## 2. LangChain / LangGraph 版本决策（重要）

**背景**：LangChain 于 2025-10 发布 1.0 GA；`langgraph` 现为 1.2.x（要求 langchain-core 1.4.x，强制 pydantic v2）。
网上大量 0.2/0.3 时代教程 API（`BaseMessage.tool_calls` 手写解析循环、`ToolExecutor` 等）**已过时**，切勿照搬。

本项目采用 1.x 规范写法：

```python
from langgraph.graph import START, StateGraph, END
from langgraph.prebuilt import ToolNode

graph.add_node("intent", intent_node)
graph.add_node("tools", ToolNode(ALL_TOOLS))
graph.add_conditional_edges("intent", route_after_intent, {...})
```

- 工具调用走 **`llm.bind_tools(...) + ToolNode` 官方循环**，不手写解析
- Chroma 集成已从 langchain-community 拆分为独立包 **`langchain-chroma`**；community 仅用于 Document Loader

## 3. LLM 多 Provider 策略

统一由 `config/llm_factory.py` 的 `create_llm()` 分发，业务代码只依赖 `BaseChatModel`：

| Provider | 实现 | 说明 |
|---|---|---|
| `deepseek`（默认） | `ChatDeepSeek`（langchain-deepseek 官方包） | 原生 tool calling 支持；模型 `deepseek-v4-flash`（env 可改，兼容 deepseek-chat） |
| `openai` | `ChatOpenAI`（langchain-openai） | 标准 OpenAI 接入 |
| `local` | `ChatOpenAI` 指向本地 base_url | vLLM / Ollama 等 OpenAI 兼容端点，api_key 占位即可 |
| `codebuddy` | `ChatOpenAI` 指向 workbuddy2api 本地代理 | 复用 WorkBuddy 账号额度，原生 tool calling（详见 06/07 文档） |

> 注意：**DeepSeek 不提供 Embedding API**，RAG 的向量化见 05_rag.md。
> **无 Mock 红线**：未配置对应通道 Key 时 `create_llm` 直接抛 `ValueError`，不存在 mock 兜底。

## 4. 依赖安装（Python 3.13）

已在 Python 3.13 验证兼容：`chromadb 1.5.x / SQLAlchemy 2.0.51 / pandas 2.2.x / langchain 1.x 全系 / playwright 1.61`。

安装命令：

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
.venv/bin/pip install -r requirements.txt       # Linux/macOS
```

**Windows 避坑**：
- MySQL 8 默认认证 `caching_sha2_password` → 必须安装 `cryptography`（已在 requirements 中）
- chromadb 内置 ONNX 若报 DLL 错误 → 补装 `onnxruntime`

## 5. 配置管理

`config/settings.py` 基于 pydantic-settings，统一前缀 `BIZ_`，读取 `.env`：

```
BIZ_LLM_PROVIDER      # deepseek / openai / local / codebuddy（运行时切换见 /api/llm/switch）
BIZ_DEEPSEEK_API_KEY
BIZ_DB_HOST/PORT/USER/PASSWORD/NAME
BIZ_VECTOR_STORE_TYPE # chroma / milvus
BIZ_EMBEDDING_PROVIDER # chroma_default / fastembed_bge_zh / fastembed_bge_m3 / openai
BIZ_API_TOKEN         # API 鉴权 Token（配置后 /api/* 需 Bearer）
```

> 配置与代码完全解耦：换模型、换库、切环境只改 `.env`，不改代码。

## 6. 安全与治理考虑

- **自动化执行需显式授权（授权制）**：`update_campaign_budget` **只生成 dry-run 执行计划**（`plan_id`，一次性 + 10 分钟 TTL，线程安全 + 审计）；真正执行只能经 `POST /api/execute/confirm`（受 API Token 鉴权保护）。LLM 永远无法自行改库。
- **LLM 不直接操作浏览器**：强制经工具层封装，操作可审计、可回滚。
- **可观测性**：request_id 贯穿日志/审计/前端 trace；`GET /api/audit` 只读查询；工具错误信息脱敏。
