# 企业经营智能决策与自动化执行 Agent 系统

> Enterprise Intelligent Decision & Automation Agent — 面向连锁门店的智能运营闭环
> **数据获取 → 智能分析 → 知识问答 → 问题诊断 → 策略生成 → 自动执行**

用户只需用自然语言提问，系统自动完成完整闭环。系统**同时具备两大能力**：

- 📊 **经营数据分析**：理解意图 → 查询业务数据（MySQL）→ Pandas 指标计算 → 异常归因 → 检索运营知识库 → 生成经营诊断报告
- 📖 **企业内部知识问答**：直接检索知识库（制度/手册/话术/流程）→ 口语化回答

例如：

> "分析最近 7 天 1 号门店营业额下降原因，并给出优化建议。" → 经营诊断报告
> "我是新员工，每天上班时间是多少，忘记打卡怎么办？" → 知识库口语化回答

---

## 一、核心特性

| 特性 | 说明 |
|---|---|
| **Supervisor 双 Agent** | 经营分析 Agent（工具+Pandas 诊断）与知识问答 Agent（纯 RAG）独立子图，由 Supervisor 确定性路由分发 |
| **默认走 RAG** | 只有明确的数据类提问才触发工具查询，制度/流程/话术类问题默认走知识问答（避免答非所问） |
| **双 LLM 通道** | DeepSeek 直连（默认）与 CodeBuddy 通道（workbuddy2api 本地代理）并存，一行配置切换 |
| **确定性计算** | 指标由 Pandas 计算，LLM 只做归因解释与报告；意图路由用关键词判定（不依赖模型发挥） |
| **混合检索 RAG** | 向量（bge-small-zh-v1.5）+ BM25（jieba 分词）→ RRF 融合；父子切割返回上下文 |
| **多格式知识入库** | 支持 Markdown / TXT / PDF / DOCX 自动切块入库 |
| **卡片化报告** | 结论摘要/关键指标/原因归因/建议/风险提示五段卡片，KPI 卡按问题动态筛选 |
| **会话持久化** | 按 session_id 存储对话历史，刷新不丢上下文 |
| **日志审计** | chat/tool_call/rag_upload 全量审计（audit.log + audit_logs 表） |

---

## 二、快速开始

```bash
# 1. 创建虚拟环境并安装依赖（Python 3.13）
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Windows
# .venv/bin/pip install -r requirements.txt          # Linux/macOS

# 2. 配置环境变量
cp .env.example .env
#   编辑 .env：
#   - BIZ_DB_PASSWORD=<MySQL 密码>          （必填）
#   - BIZ_STORES_JSON=D:\path\stores.json    （门店主数据，可选）
#   - BIZ_LLM_PROVIDER=deepseek|codebuddy    （LLM 通道，见第四节）

# 3. 建库建表 + 灌入数据（真实门店 37 家 + 订单/推广/市场数据）
.venv/Scripts/python -m scripts.seed

# 4. 启动 LLM 通道（仅 codebuddy 通道需要；DeepSeek 直连可跳过）
cd /d/workbuddy2api && ./start-wb2api.sh start      # Git Bash

# 5. 启动服务（首次启动会下载 RAG 嵌入模型，约 90MB，一次性）
cd backend
.venv/Scripts/uvicorn main:app --reload --port 8000

# 6. 验证
curl http://127.0.0.1:8000/health
```

**当前状态**：已接入真实 MySQL（`business_agent` 库，37 家密室逃脱真实门店 + 23.3 万订单 + 128 条推广计划 + 客流/交易/咨询市场数据），`BIZ_DATA_MODE=real`。

**无任何 API Key 也可运行**：LLM 未配置时自动降级 MockLLM（数据链路仍真实、报告为占位输出），保证服务永远可启动、链路可演示。

---

## 三、对话调用

### 3.1 前端页面

浏览器打开 `http://127.0.0.1:8000/`，内置 8 个示例问题（📊 经营分析 4 个 + 📖 内部制度 4 个），支持 SSE 打字机流式输出、五段卡片化报告、按问题筛选的 KPI 指标卡。

### 3.2 API

```bash
# 普通对话（非流式）
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "分析最近7天1号门店营业额下降原因并给出优化建议"}'

# 流式对话（SSE：progress/token/done 事件）
curl -N -X POST http://127.0.0.1:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "门店晋升需要什么条件", "session_id": "abc"}'

# 知识库文档上传（md/txt/pdf/docx，自动入库）
curl -X POST http://127.0.0.1:8000/api/rag/upload \
  -F "file=@门店员工手册.pdf" -F "doc_type=hr"

# 数据采集（前端按钮驱动，刷新美团经营数据入库）
curl -X POST http://127.0.0.1:8000/api/workflow/refresh \
  -H "Content-Type: application/json" -d '{"datasets": "all"}'

# 会话管理
curl http://127.0.0.1:8000/api/sessions
```

### 3.3 API 端点一览

| 端点 | 说明 |
|---|---|
| `GET /health` | 健康检查（含 LLM 通道/数据模式/数据库状态） |
| `POST /api/chat` | 非流式对话（返回 report + trace + usage） |
| `POST /api/chat/stream` | SSE 流式对话（节点进度 + 打字机） |
| `GET /api/sessions` / `POST /api/sessions/{id}` | 会话列表 / 指定会话 |
| `POST /api/rag/upload` | 知识库文件上传入库 |
| `POST /api/workflow/refresh` | 数据采集（智选展位/客流/交易/咨询） |

---

## 四、LLM 双通道配置

系统内置两套 LLM 通道，通过 `.env` 的 `BIZ_LLM_PROVIDER` 一行切换：

### 通道 A：DeepSeek 直连（默认）

```ini
BIZ_LLM_PROVIDER=deepseek
BIZ_DEEPSEEK_API_KEY=sk-xxx
BIZ_DEEPSEEK_MODEL=deepseek-v4-flash
```

官方 OpenAI 兼容接口，原生 tool calling，无需任何本地服务。

### 通道 B：CodeBuddy（workbuddy2api 本地代理）

```ini
BIZ_LLM_PROVIDER=codebuddy
BIZ_CODEBUDDY_BASE_URL=http://127.0.0.1:8787/v1
BIZ_CODEBUDDY_MODEL=deepseek-v4-flash   # 可用: glm-5.2 / glm-5.1 / kimi-k2.7 / deepseek-v4-flash / hy3-preview-agent
BIZ_CODEBUDDY_API_KEY=
```

复用本机 **WorkBuddy/CodeBuddy 桌面端登录态**，把账号额度转成 OpenAI 兼容接口（**支持原生 tool calling**），无需 API Key、无需 Docker。

```bash
# 管理服务（Git Bash）
cd /d/workbuddy2api
./start-wb2api.sh start|status|stop|restart|logs
```

> 依赖登录态文件 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`；
> token 过期时在 WorkBuddy 桌面端重新登录即可。详见 `docs/06_codebuddy_channel.md`。

### ⚠️ 关于 codebuddy-proxy（19090）

cnb.cool 的 `codebuddy-proxy`（Docker 部署，`http://127.0.0.1:19090/v1`）**不支持原生 tool calling**（CLI/HTTP 后端均实测），**只能用于 Chatbox 等纯聊天客户端**，不可接入本 Agent 的工具循环。需要 agent 工具调用时请使用 workbuddy2api 通道。

---

## 五、系统架构

### 5.1 Supervisor 多 Agent

```
用户问题
  │
  ▼
┌──────────────┐   命中 DATA_KEYS（营业额/订单/环比/推广/客流/排名/报表…）
│  Supervisor  ├───────────────────────────►  📊 经营分析 Agent（data_agent 子图）
│  确定性路由   │
└──────┬───────┘   未命中（制度/手册/流程/话术/考勤…）
       │
       └───────────────────────────────────►  📖 知识问答 Agent（kb_agent 子图）
```

- **经营分析 Agent**（`agent/data_agent.py`）：`intent(工具决策) → [tools ⇄ intent 回环] → analysis(Pandas) → rag → report(经营诊断)`
- **知识问答 Agent**（`agent/kb_agent.py`）：`rag(检索) → report(口语化知识回答)` —— 无工具、无经营分析、报告不入经营经验层
- 两个子图独立演进，后续加第三个 Agent（如自动执行）只需在 Supervisor 增加一条路由

### 5.2 意图路由（默认 RAG）

- 命中 `DATA_KEYS`（指标导向关键词）→ 数据链路（工具查询真实数据）
- 未命中 → 默认知识问答链路（不调数据工具，直接检索知识库回答）
- 判定为**确定性关键词匹配**（`agent/nodes.py::is_data_question`），不依赖 LLM 发挥

### 5.3 工具清单（9 个，`tools/ALL_TOOLS`）

| 工具 | 职责 | 数据源 |
|---|---|---|
| `get_sales_data` | 营业额/订单数/客单价/环比 | MySQL |
| `get_campaign_data` | 推广花费/点击/转化/ROI | MySQL |
| `get_traffic_data` | 客流（曝光/访问/意向转化） | MySQL |
| `get_transaction_data` | 交易（下单/核销/退款） | MySQL |
| `get_consult_data` | 在线咨询（人数/留咨/回复率） | MySQL |
| `get_store_ranking` | 门店综合排名（客流+交易+咨询加权） | MySQL |
| `analysis_business_data` | 指标计算与归因（Pandas 确定性计算） | — |
| `search_operation_knowledge` | 检索运营知识库 | Chroma |
| `update_campaign_budget` | 调整推广预算（自动化执行，**需用户授权 confirm=True**） | Playwright |

> 数据采集（`refresh_market_data`）不注册为 LLM 工具——改为**前端按钮驱动**（`/api/workflow/refresh`），避免对话触发的高成本与不可控。

### 5.4 请求链路（数据类为例）

```
FastAPI(/api/chat/stream)
  → Supervisor 路由
  → 经营分析 Agent: intent(LLM 选工具) → get_sales_data/get_campaign_data → tools_node(完整结果入 state.query_result)
  → analysis(analysis_business_data, Pandas 计算指标+归因)
  → rag(知识层纯问题检索 + 经验层带分析结论检索)
  → report(LLM 生成五段卡片报告)
  → 报告自动入库（经验层，doc_type=report，30 天有效期）
```

---

## 六、目录结构

```
Business Agent/
├── README.md
├── docs/                              # 6 份设计文档
│   ├── 01_architecture.md             #   架构说明
│   ├── 02_tech_stack.md               #   技术选型
│   ├── 03_database.md                 #   数据库设计
│   ├── 04_agent_flow.md               #   Agent 流程
│   ├── 05_rag.md                      #   RAG 设计
│   └── 06_codebuddy_channel.md        #   CodeBuddy 通道接入（workbuddy2api）
└── backend/
    ├── main.py                        # FastAPI 入口
    ├── requirements.txt / .env.example
    ├── config/
    │   ├── settings.py                # 统一配置（BIZ_ 前缀，pydantic-settings）
    │   ├── llm_factory.py             # LLM 工厂（deepseek/openai/local/codebuddy/mock）
    │   └── logging_setup.py           # 日志滚动 + 审计
    ├── api/chat.py                    # /api/chat、/stream、/sessions、/rag/upload、/workflow/refresh
    ├── agent/
    │   ├── graph.py                   # Supervisor 主图（路由分发）
    │   ├── data_agent.py              # 经营分析 Agent（子图 A）
    │   ├── kb_agent.py                # 知识问答 Agent（子图 B）
    │   ├── nodes.py                   # 节点实现 + 三套系统提示词 + DATA_KEYS 路由词表
    │   └── state.py                   # AgentState（含 intent_type 路由标记）
    ├── tools/                         # 9 个工具（数据/分析/RAG/执行）
    ├── rag/
    │   ├── loader.py                  # md/txt/pdf/docx 加载 + 父子切割
    │   ├── embedding.py               # chroma 默认 / fastembed bge-zh / openai 可切换
    │   └── retriever.py               # 向量 + BM25 混合检索（RRF）+ 经验层时间衰减
    ├── database/                      # models / mysql（SQLAlchemy 2.x）
    ├── data/stores.json               # 真实门店主数据快照（37 家）
    ├── static/index.html              # 前端单页（示例问题/流式/卡片化/KPI）
    └── scripts/seed.py                # 建库建表 + 灌入真实门店与业务数据
```

---

## 七、RAG 知识库

| 项 | 配置 |
|---|---|
| 向量库 | Chroma（`backend/chroma_db/`；Milvus 抽象层预留，`BIZ_VECTOR_STORE_TYPE` 切换） |
| Embedding | **fastembed bge-small-zh-v1.5**（512 维，中文效果好；`BIZ_EMBEDDING_PROVIDER=fastembed_bge_zh`） |
| 切块策略 | **父子切割**：父块 1200 字 / 子块 350 字，命中子块返回父块上下文 |
| 混合检索 | 向量 + jieba 分词 BM25 → RRF 融合（k=60），中文查询显著提升召回 |
| 经验层 | 历史诊断报告自动入库（doc_type=report），30 天有效期 + 时间衰减 |
| 入库格式 | Markdown / TXT / PDF（pypdf 文本提取）/ DOCX（python-docx 段落+表格） |

内置知识（启动自动 ingest，约 94 chunks）：满意度回访话术、门店晋升制度、门店薪资绩效管理办法、员工手册（考勤章节）。

---

## 八、配置项（.env 全量）

```ini
# ---------- LLM ----------
BIZ_LLM_PROVIDER=deepseek          # deepseek / openai / local / codebuddy / mock
BIZ_DEEPSEEK_API_KEY=sk-xxx
BIZ_DEEPSEEK_MODEL=deepseek-v4-flash
BIZ_OPENAI_API_KEY=
BIZ_OPENAI_MODEL=gpt-4o-mini
BIZ_LOCAL_BASE_URL=http://localhost:11434/v1
BIZ_LOCAL_MODEL=qwen2.5:14b
BIZ_CODEBUDDY_BASE_URL=http://127.0.0.1:8787/v1
BIZ_CODEBUDDY_MODEL=deepseek-v4-flash
BIZ_CODEBUDDY_API_KEY=
BIZ_LLM_TEMPERATURE=0.3

# ---------- MySQL ----------
BIZ_DB_HOST=127.0.0.1
BIZ_DB_PORT=3306
BIZ_DB_USER=root
BIZ_DB_PASSWORD=<你的密码>
BIZ_DB_NAME=business_agent
BIZ_STORES_JSON=D:\path\stores.json

# ---------- RAG ----------
BIZ_VECTOR_STORE_TYPE=chroma       # chroma / milvus
BIZ_CHROMA_DIR=./chroma_db
BIZ_EMBEDDING_PROVIDER=fastembed_bge_zh   # chroma_default / fastembed_bge_zh / openai

# ---------- 数据模式 ----------
BIZ_DATA_MODE=real                 # mock / real

# ---------- 自动化执行（阶段二） ----------
BIZ_PLAYWRIGHT_HEADLESS=true
BIZ_OPS_PLATFORM_URL=http://localhost:3000/ops
```

---

## 九、运维与排障

### 9.1 服务启停

| 服务 | 端口 | 启动 |
|---|---|---|
| 项目后端 | 8000 | `cd backend && .venv/Scripts/uvicorn main:app --reload --port 8000` |
| workbuddy2api（LLM 通道） | 8787 | `cd /d/workbuddy2api && ./start-wb2api.sh start` |
| codebuddy-proxy（可选，纯聊天） | 19090 | `cd /d/codebuddy-proxy && ./start-proxy.sh start` |

### 9.2 常见问题

| 现象 | 处理 |
|---|---|
| 对话答非所问（知识问题变数据报告） | 检查问题是否误命中 DATA_KEYS；`/api/chat/stream` 的 progress 显示路由方向 |
| 报告空白但显示"✓ 完成" | Supervisor+子图下必须用 `updates` 模式流式（已实现）；确认 `report_node` 流式正常 |
| 报告偶现内部字段名（如 `reply30=96.61%`） | `analysis_business_data` 返回值的字段被模型原样引用，属已知小瑕疵，后续清洗 |
| workbuddy2api 502 `All connection attempts failed` | 服务连接异常，`./start-wb2api.sh restart` 后重试 |
| LLM 自动降级 Mock | `.env` 未配置对应通道 Key；`/health` 的 `llm_provider` 与 `mock_mode` 可确认 |
| 中文 curl 乱码 | Windows 下用 `--data-binary @file`（UTF-8 文件）发送 |

### 9.3 日志与审计

- `backend/logs/app.log`：按天滚动业务日志
- `backend/logs/audit.log` + `audit_logs` 表：chat 请求、tool_call、rag_upload 审计

---

## 十、演进路线

| 阶段 | 状态 | 内容 |
|---|---|---|
| **Phase 1** | ✅ 完成 | MySQL 真实数据 + LLM 双通道 + Chroma 混合检索 RAG 全接通 |
| **Phase 2** | 🚧 进行中 | Supervisor 多 Agent（✅ 已上线：经营分析 + 知识问答）· Playwright 自动执行（骨架，授权制）· Docker 部署 |
| **Phase 3** | 📋 规划 | 自动执行 Agent 独立化 · 多轮追问澄清 · 知识库内容运营（完整员工手册入库） |

## 十一、技术栈

Python 3.13 · FastAPI · LangGraph 1.2 / LangChain 1.x · DeepSeek / CodeBuddy(workbuddy2api) / OpenAI / 本地模型 ·
SQLAlchemy 2 + MySQL · Pandas/NumPy · Chroma + fastembed(bge-zh) + BM25 · Playwright（阶段二）· Docker（阶段二）

> ⚠️ **LangChain 已 1.0 GA**：本项目统一使用 1.x API（`StateGraph` + subgraph + ToolNode），勿参考 0.2/0.3 旧教程。
> 版本锁定：langgraph==1.2.*、langchain==1.3.*、langchain-deepseek==1.1.*、langchain-community>=0.3,<1.0（community 未跟随 1.0 版本号）。
