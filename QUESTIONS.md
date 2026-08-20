# Business-Agent 项目问题与优化记录

> 本文档记录项目开发过程中遇到的问题、修复方案、功能优化。
> 每次修改或优化都追加新条目，按时间倒序排列（最新在上）。

---

## 📋 目录

- [已修复问题](#已修复问题)
- [功能优化](#功能优化)
- [已知问题 / 待优化](#已知问题--待优化)

---

## 已修复问题

### 2026-08-20 | 销量排名数据路由与 OpenAI-compatible 流式重复修复

- **现象/背景**：用户询问“最近 7 天门店销量最多的是哪一家”被默认分到知识问答，错误检索内部制度文档；部分 OpenAI-compatible 流式端点返回累计文本块，前端最终显示同一段回答两遍。
- **根因/修改**：`销量/销售量`未进入 `DATA_KEYS`，且无跨门店销售排名工具；新增 `get_store_sales_ranking`，以 MySQL `orders` 表按门店聚合销量、订单数和 GMV，确定性规划优先调用它。纯数据事实查询不再检索知识库，只有“原因/建议/优化”等诊断类数据问题才检索运营知识。新增后端 `_merge_stream_text()` 与前端 `mergeStreamText()`，兼容累计流式文本而不重复拼接。
- **影响范围**：销量、订单量、营业额等跨门店排名问题；OpenAI-compatible 流式知识问答显示。
- **验证/决策**：新增路由、工具计划和累计流去重测试；完整测试 `46 passed`。Docker 重新构建后即可生效。

### 2026-08-20 | Ragas 真实数据评测与 LangChain 兼容性修复

- **现象/背景**：Ragas 评测入口已提交但未安装开发依赖，无法确认当前向量库的真实上下文质量；Ragas 0.2.15 仍引用已从 `langchain-community 0.4.x` 移除的 `chat_models.vertexai` 模块。
- **根因/修改**：安装 `ragas==0.2.15`、`datasets` 等开发依赖；在 `scripts/eval_ragas.py` 增加仅作用于评测进程的 `ChatVertexAI` 兼容导入层，避免降级生产 LangChain 依赖；新增 `docs/10_ragas_eval_2026-08-20.md` 记录结果。
- **影响范围**：仅 Ragas 开发评测，不改变生产 Docker 镜像和线上 LLM 通道。
- **验证/决策**：基于当前 Chroma 与 21 条真实 golden set 完整评测，`Hit@5=90.5%`、`context_precision=0.5865`、`context_recall=0.7672`；少量 judge 请求超时，后续趋势比较需固定模型与超时策略。

### 2026-08-20 | OpenAI-compatible 通道命名、Docker 生产骨架与 Ragas 评测入口

- **现象/背景**：项目使用第三方 OpenAI-compatible 模型时复用 `openai` 配置名，容易误导；部署仍依赖 Windows 启动脚本，缺少 Linux 容器、数据卷和反向代理方案；RAG 只有 Hit@k 脚本，缺少上下文精度/召回的统一评测入口。
- **根因/修改**（`config/settings.py`、`config/llm_factory.py`、`api/chat.py`、`static/index.html`、`Dockerfile`、`docker-compose.yml`、`deploy/nginx.conf`、`scripts/eval_ragas.py`、`docs/09_docker_deployment.md`）：
  1. 新增 `openai_compatible` provider，支持独立的 `BIZ_OPENAI_COMPATIBLE_*` 配置；旧 `openai` 保留兼容映射，第三方模型不再被标识为官方 OpenAI；
  2. 新增 Python 3.13 Docker 镜像、`app/mysql/nginx` Compose、Chroma/RAG/模型缓存卷和 SSE 反代配置；Windows Edge/Playwright、CodeBuddy、Milvus、Redis 不进入首版 Linux 部署；
  3. 补充 `python-docx` 运行依赖，避免启动时加载 DOCX 知识文档失败；
  4. 新增可选 `ragas` 开发依赖和 `scripts/eval_ragas.py`，评估 Context Precision/Context Recall；保留 `scripts/eval_rag.py` 的 Hit@k 作为来源命中回归指标。
- **影响范围**：第三方 LLM 配置、Linux 部署、RAG 评测和运行依赖；现有 DeepSeek、旧 `openai` 配置仍可兼容运行。
- **验证**：已完成 Python 语法级校验与配置静态检查；Docker 构建和真实 Ragas 评测需在安装 Docker/项目依赖、准备 MySQL 与 LLM Key 的环境执行。

### 2026-08-19 | 前端报告渲染修复：第三方流式模型缺字/错乱 + 市场类问题 ¥0 KPI 误导

- **现象/背景**：① 第三方 OpenAI 兼容流式模型（如商汤 SenseNova）的 token chunk 序列与最终内容不一致（缺字/换行丢失），前端打字机按增量渲染得到"回答错乱/重复"；② 排名/客流/交易等市场类问题不查销售数据，`metrics` 全 0，前端渲染出 ¥0 卡误导（正文已含排名表）。
- **根因/修改**（`static/index.html`）：
  1. done 事件携带的 `report` 为后端权威完整文本——最终渲染优先用 `m.report` 覆盖打字机增量，打字机仅作过程动画，保证历史持久化/导出内容与展示一致；
  2. `renderKpis` 在 `gmv/order_count/avg_order_value` 全为空/0/NaN 时返回空字符串（隐藏 ¥0 KPI 卡）。
- **影响范围**：前端报告渲染；kb/data 双链路 final report 均以 done.report 为准。
- **验证**：商汤通道对话正常渲染完整文本；市场类问题不再显示 ¥0 KPI 卡。

### 2026-08-19 | Windows GBK 控制台输出 UTF-8 兜底（打印 ✓/✅ 崩溃）

- **现象/背景**：Windows 控制台默认 GBK 编码，脚本/服务打印 `✓/✅/❌` 等非 GBK 字符直接抛 `UnicodeEncodeError` 崩溃（实测于数据采集流程）。
- **根因/修改**：`config/logging_setup.py::setup_logging` 与 `scripts/import_login_state.py` 入口处对 stdout/stderr 执行 `reconfigure(encoding="utf-8", errors="replace")`；重定向/非 TextIOWrapper 场景跳过。
- **影响范围**：服务启动与数据采集脚本的中文/emoji 打印稳定性。
- **验证**：数据采集流程（Edge 拉起/登录态注入/下载导入）全程打印正常，不再崩溃。

### 2026-08-14 | 数据问答链路降本提速（确定性规划 + 上下文压缩 + RAG 按需改写）

- **现象/背景**：常规数据问题即使最终会由 `analysis_node` 兜底查询，仍先经过一次 LLM 工具决策及可能的 ReAct 回环；会话历史与完整数据明细又会重复注入模型。知识问答则每次都执行 Query Rewrite + HyDE，明确制度名也要额外消耗一次 LLM，导致数据回复慢、token 偏高。
- **根因/修改**（`agent/nodes.py`、`agent/state.py`、`tests/test_performance_paths.py`）：
  1. 新增 `_build_data_tool_plan()`：营业额/订单类只查销售，推广类只查推广，排名类查对应市场指标；支持“最近 N 天/本月/昨天”时间窗，明确起止日期、预算调整和执行等高风险计划仍保留 LLM tool calling，避免猜测日期、campaign 或预算参数；
  2. `AgentState` 新增 `tool_plan`，确定性计划执行后清空，防止回边重复查询；市场问题不再额外兜底查单店销售数据；
  3. 数据工具规划模型只接收当前问题，不再携带历史报告；报告输入把销售数据压缩为 summary、近 7 日趋势、Top 3 商品、Top 5 品类，推广计划限制为 Top 3；数据报告最大输出由 3000 收紧到 1200 tokens；
  4. kb 链路先以原问题检索；至少 2 条结果且最高相似度 ≥0.70 时直接生成回答，只有低置信/未命中才调用 Query Rewrite + HyDE 并行补召回。
- **影响范围**：数据类首响、LLM 输入/输出 token、MySQL 聚合次数与知识问答延迟；预算调整等需确认的自动化路径保持原有安全语义。
- **验证**：新增 `tests/test_performance_paths.py`，覆盖销售/推广/预算调整的查询规划与报告数据压缩；`python -m py_compile agent/nodes.py agent/state.py` 通过。当前全局 Python 缺少 `langchain_core`，pytest 在收集阶段无法导入项目依赖，待按 `requirements.lock` 建立项目虚拟环境后执行完整测试。

### 2026-08-14 | 报告结构化输出（#14，告别正则兜底与字段泄漏）

- **现象/背景**：data 链路报告五段结构靠"prompt 自律 + 前端宽松正则"兜底，模型偶发吞字、泄漏内部字段名（如 `reply30=96.61%`，见 08-11 已知问题）。
- **根因/修改**（`agent/nodes.py`、`api/chat.py`、`static/index.html`、`agent/state.py`）：
  1. 新增 `ReportSections` schema（summary/metrics/factors/actions/risks）+ `REPORT_STRUCTURED_PROMPT`；
  2. `_report_structured` 三级策略：① `with_structured_output`（function calling）→ ② **prompt 输出 JSON + `_parse_report_sections` 手动解析**（`_split_items` 容错字符串/数组）→ ③ 流式 markdown 回退。实测 deepseek-v4-flash **thinking mode 不支持 tool_choice**（400），自动走方案二；
  3. `AgentState` 增加 `report_sections` 字段（否则 LangGraph 丢弃该 key，SSE 拿不到）；
  4. SSE done 事件 + 非流式响应透传 `report_sections`；前端有 sections 时直接渲染五段卡（KPI 卡仍取 Pandas metrics），无则回退 markdown 渲染；
  5. 数据刷新前缀、经验层入库用确定性 `_sections_to_markdown` 生成的 markdown。
- **影响范围**：data 链路报告生成与前端渲染；kb 链路保持口语化 markdown（不受影响）。
- **验证**：真实对话"分析最近7天1号门店营业额下降原因"→ done 事件 report_sections 五段齐全（summary 3/metrics 4/factors 3/actions 3/risks 2），metrics 真实（gmv=99924、环比-71.03%）；结构化失败自动回退流式 markdown（日志可查）。

### 2026-08-14 | 真流式 SSE（#5，astream_events 透传子图 LLM token）

- **现象/背景**：旧实现 `stream_mode="updates"`（节点级）+ final_report 就绪后按 6 字/帧模拟打字机——**首 token 延迟 = 全量生成时间**（8s 生成期用户看不到输出），且 usage 靠 `报告字数×12/×3` 粗估（`input_tokens/output_tokens` 从未真实更新）。
- **根因/修改**（`api/chat.py`）：改用 `agent.astream_events(version="v2")`：
  - `on_chat_model_stream` + `metadata.langgraph_node=="report"` → **逐 token 透传**（kb 链路真流式；data 链路为结构化 JSON 不透传，避免污染打字机）；
  - `on_chat_model_end` → 采集真实 token_usage（DeepSeek 流式不返回时仍估算并标注 estimated）；
  - supervisor 事件注意：会触发两次 on_chain_end（首次 output 为条件边字符串 `"data"`，二次才是节点 dict），仅 dict 且含 intent_type 时采用；
  - `on_chain_end`（data_agent/kb_agent）取完整 state（metrics/factors/pending_plans/final_report/report_sections）；
  - error 事件后追加 `retry: 3000`（SSE 重连提示，#8）。
- **影响范围**：流式对话体验与 token 统计；非流式接口不变。
- **验证**：kb 问题实测 **69 个真实 token 事件**（打字机）+ progress ×3 + done 结构完整；data 链路 token 事件 0（结构化）+ done.report_sections 完整。

### 2026-08-14 | 意图路由统一配置 + 门店名解析（#6）

- **现象/背景**：① 门店/指标词无法穷举（"XX店营业额"只能猜 store_id=1）；② 双列表命中冲突（"报销流程数据"同时命中知识词"报销"与数据词"数据"）；③ supervisor 与 report 的判定逻辑不同步（`report_node` 里 `is_knowledge = not is_data_question(...) or any(k in KNOWLEDGE_KEYS)` 与 supervisor 的 `is_data_question` 不一致）。
- **根因/修改**：
  1. 新建 `agent/routing.py`：`resolve_intent()` **知识词优先**（命中知识词→kb；否则数据词→data；未命中→kb），`DATA_KEYS/KNOWLEDGE_KEYS/MARKET_KEYS` 收敛一份；supervisor / report_node / analysis_node 全部改用该函数（`is_data_question` 保留为兼容包装）；
  2. 新建 `tools/store_resolver.py`：门店名→store_id（DB `stores` 表权威，stores.json 序号回退；"N号门店"正则 + 名称/search_keyword 模糊匹配）；supervisor 解析后写入 `state.store_id`，intent_node 注入 LLM 提示、analysis_node 兜底查询用它、经验层检索按门店隔离。
- **影响范围**：路由判定、报告模板选择、门店定位；所有消费方共用一份配置。
- **验证**：路由矩阵单测通过（含冲突用例"报销流程数据"→kb）；"1号门店"问题实测 store_id=1 注入。

### 2026-08-14 | 数据缓存 + LLM 实例复用 + kb 检索并行（#7）

- **现象/背景**：① 每请求实时聚合近 7 天 23.3 万订单（无缓存）；② `create_llm()` 每次调用新建实例；③ kb 链路 4 路检索串行（kb 问答 8.6s 的重要构成）。
- **根因/修改**：
  1. 新建 `tools/data_cache.py`（TTL 60s + 线程安全 + deepcopy）；`get_sales_data/get_campaign_data/get_traffic_data/get_transaction_data/get_consult_data/get_store_ranking` 全部接入；`/api/workflow/refresh` 成功与 `confirm_plan` 执行成功后 `invalidate_data_cache()`；
  2. `config/llm_factory.py` 按 provider 缓存复用实例（`_instances` dict）+ 运行时切换（#15）；
  3. `rag_node` kb 4 路检索改 `asyncio.gather` 并行（to_thread + 串行 fallback）。
- **影响范围**：查询性能、LLM 连接开销、kb 延迟。
- **验证**：单测（缓存读写/失效）通过；kb 对话端到端 7.4s（含 rewrite + 真流式）。

### 2026-08-14 | 可观测性：request_id 贯穿 + /api/audit + 错误脱敏（#8）

- **现象/背景**：多处 `except: pass` 吞异常；审计只有文件/表写入无查询接口；SSE 出错只发 error 无重连提示；工具失败 `str(exc)` 可能含 SQL/密钥细节暴露给 LLM/前端。
- **根因/修改**：
  1. 新建 `config/request_id.py`（contextvars + `RequestIdFilter`），`logging_setup` formatter 带 `[request_id]`；chat/chat_stream 入口生成，审计 payload 自动携带，SSE done 事件回传（前端 meta 展示）；
  2. 新增 `GET /api/audit`（时间/事件类型/会话过滤，limit≤500，detail 脱敏）；
  3. 新建 `tools/sanitize.py`（掩码 password/token/key 等 + 连接串凭据 + 截断 200 字）；`tools_node` 与 SSE error 事件使用；
  4. SSE error 后追加 `retry: 3000`。
- **影响范围**：日志/审计/前端 trace 全链路；错误信息不再泄露敏感细节。
- **验证**：`/api/audit` 实测返回审计条目（含 request_id）；sanitize 单测通过（`password=abc123`→`password=***`）。

### 2026-08-14 | RAG 检索性能：BM25 缓存 + 父块独立存储（#9）

- **现象/背景**：① BM25 每次查询 `col.get(...)` 全量拉取整个 collection（O(N)）；② 父子切割把 parent_content 冗余存进每个子块（存储膨胀）；③ `SCORE_THRESHOLD` 注释仍写"all-MiniLM 中文偏低"，与实际 bge-zh 不符。
- **根因/修改**（`rag/retriever.py`、`rag/loader.py`）：
  1. `_bm25_corpus()` 语料按过滤条件缓存（TTL 300s + 缓存键 json 序列化），`add_documents/delete` 后自动 `invalidate_cache()`；
  2. 父块全文抽独立 `parent_docs` collection（按 parent_id upsert 幂等），子块 metadata 只存 parent_id，命中后 `_expand_parent` 回查（兼容旧数据 parent_content）；
  3. SCORE_THRESHOLD 注释修正（bge-zh 0.5~0.6 波动）；`embedding.py` 新增 `fastembed_bge_m3` 选项。
- **影响范围**：检索延迟与存储体积；**需删除 chroma_db 重建向量库**（新结构：父块独立存储 + 512 维 bge-zh）。
- **验证**：重建后 99 chunks；RAG 评测 Hit@5=90.5%（19/21）；单测 36 passed。

### 2026-08-14 | 知识上传接口安全（#2，路径穿越 + 幂等粒度错误）

- **现象/背景**：`rag_upload` 直接拼接上传文件名（`../../x` 路径穿越风险）、无大小/类型限制、幂等删除按 doc_type 清库（上传 general 文档会清空所有 general 文档）。
- **根因/修改**（`api/chat.py` + `rag/loader.py::upload_and_ingest`）：文件名取 basename + 白名单后缀（md/txt/pdf/docx）+ ≤20MB + 内容非空校验；幂等删除改按 `source`（文件名）精确清理，不再按 doc_type。
- **影响范围**：知识上传接口安全；上传行为不影响其他文档。
- **验证**：无法上传非法类型/超限/空文件；同名覆盖、异名并存（代码路径 + 单测未覆盖上传，接口由 selftest 的 main 导入保证可加载）。

### 2026-08-14 | 经验层同日报告互相覆盖（#3）

- **现象/背景**：`_ingest_report_to_kb` 幂等删除按 `report_date` 全清——同一天不同门店/不同问题生成的多份报告互相覆盖，只留最后一份。
- **根因/修改**（`agent/nodes.py` + `rag/retriever.py`）：report 文档新增 `report_id`（日期+门店+问题哈希指纹）；幂等删除粒度改为 `report_date + store_id + report_id`（同店同日同问题才覆盖）；检索端经验层按 `filter_meta={"store_id"}` 隔离门店。
- **影响范围**：经验层入库与检索；跨门店历史报告不再互相污染。
- **验证**：同问题重问覆盖、不同问题各自保留（逻辑单测 + 日志）。

### 2026-08-14 | 环境坑：HTTP_PROXY=127.0.0.1:10090 导致 LLM 间歇性 Connection error

- **现象/背景**：服务内对话偶发 `openai.APIConnectionError: Connection error.`，但独立脚本直连成功。
- **根因/修改**：开发机环境注入 `HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:10090`（WorkBuddy 本地代理，间歇不可用）；httpx 默认 trust_env=True 走该代理。`config/llm_factory.py::_http_client()` 显式 `httpx.Client(trust_env=False, timeout=120/30)` 传给所有 provider 实例。
- **影响范围**：LLM 通道稳定性（deepseek/codebuddy/local/openai 全部直连）。
- **验证**：服务进程内诊断端点 invoke/stream 均成功；kb/data 对话连续多次稳定。

### 2026-08-14 | 环境坑：fastembed 缺失 → 向量库 384 维降级重建（512 维 bge-zh）

- **现象/背景**：`.env` 配 `fastembed_bge_zh` 但本机 venv 缺 fastembed → `create_embeddings` 回退 chroma_default（384 维 all-MiniLM），中文检索差一档；RAG 评测报 `Collection expecting 384, got 512`。
- **根因/修改**：安装 `fastembed`（onnxruntime 依赖）；**删除 `chroma_db/` 重建**（启动/ingest 自动从 rag/data 重建 512 维 bge-zh + 父块独立存储）。
- **验证**：重建 99 chunks；Hit@5 90.5%；`scripts/selftest_security.py` 23/23 全绿（selftest 假模块注入改为"真包已装则跳过"）。

### 2026-08-14 | 自动化执行授权机制 + API Token 鉴权（安全加固）

- **背景**：① `update_campaign_budget` 的 `confirm=True` 由 LLM 自行决定——模型可能幻觉中直接改库，无真正的用户授权；② 服务无鉴权，任何能访问 8000 端口的人都可调 `/api/chat` 消耗 LLM 额度、触发 `/api/workflow/refresh`（重启 Edge/注入登录态）、确认改库。
- **修改**：
  1. **执行计划授权机制**（`tools/execution_plans.py` 新建 + `tools/browser_tool.py` 重写）：`update_campaign_budget` 改为**只生成 dry-run 执行计划**（返回 `plan_id`，一次性 + 10 分钟 TTL + 线程安全 + 审计 `execute_plan_created`）；旧 `confirm` 参数废弃并忽略，**工具永远不直接修改数据**；真正执行只能经 `POST /api/execute/confirm {plan_id}`（`execute_plan_confirmed` 审计），锁内预留状态防并发双击重复执行。
  2. **API Token 鉴权**（`config/auth.py` 新建 + `main.py` 注册）：配置 `BIZ_API_TOKEN` 后所有非公开路径（`/api/*`、`/docs`）必须携带 `Authorization: Bearer <token>` 或 `X-API-Token`，`hmac.compare_digest` 常数时间比较；公开白名单 `/`、`/static/*`、`/health`；未配置时放行但启动/首请求告警。
  3. **链路透传**：`agent/state.py` +`pending_plans`；`tools_node` 捕获计划入 state；`_build_report_input` 带 `pending_plans` 并新增 `REPORT_SYSTEM_PROMPT` 第 4 条规则（提示用户确认/忽略）；SSE done 事件与非流式响应均返回 `pending_plans`；`GET /api/execute/plans` 列出待确认计划。
  4. **前端**（`static/index.html`）：右上角 API Token 输入（localStorage 持久化），所有请求带鉴权头、401 提示；报告下方渲染「待确认执行计划」卡片（确认执行/忽略按钮，确认后显示执行结果）。
- **影响范围**：自动化执行链路（工具/API/前端）、全部 `/api/*` 接口鉴权；对话与 RAG 链路不受影响（响应结构仅新增 `pending_plans` 字段）。
- **验证**（`python scripts/selftest_security.py`，全部通过）：TestClient + 真实 MySQL（campaign 1，测试后还原预算）——公开路径 200 / 无 token 401 / 错误 token 401 / Bearer & X-API-Token 正确 200 / `/docs` 401；create_plan dry-run 不改数据 → confirm_plan 更新预算 → 重复确认拒绝 → 现场还原；不存在 campaign/plan_id 拒绝；工具层旧 `confirm=True` 仅生成计划不执行；负数预算拒绝；`tools_node` 捕获计划入 `state.pending_plans`、报告输入含 `pending_plans`。真实服务（uvicorn + `BIZ_API_TOKEN=test-token-123`）HTTP 实测鉴权与 `/api/execute/*` 均正常（LLM 通道 8788 未启动时聊天报 `Connection error` 属环境问题，与本次改动无关）。

### 2026-08-13 | 知识问答检索不到"公司有多少门店"（Query Rewrite + HyDE 根治）

- **现象/背景**：新员工问"公司有多少门店"，回答"知识库暂无资料"。但《公司背景.docx》明确写了"全国拥有 30 多家直营和合作门店"。检索环节把该 chunk 丢了。

- **根因（实测诊断，三层）**：
  1. **不是 chunk 太大**：公司背景仅 2 个 chunk（248/279 字），大小合理，可排除。
  2. **向量相似度匹配的是"门店"共现，不是"门店数量"意图**：`bge-small-zh` 对疑问句"公司有多少门店"与陈述句"全国拥有30多家直营和合作门店"的相似度 < 0.6，纯向量 top8 一个公司背景 chunk 都没进；且全库"门店"出现几百次（员工手册/薪资/晋升/历史报告），被"销售额前五门店"等 chunk 挤占。
  3. **BM25 被高频词带偏**：query 分词后核心实词仅"门店"（库内高频词 IDF 低），员工手册里"门店"出现最多 → BM25 top8 全是员工手册。

- **顺带发现真 bug**：kb 链路 `_build_report_input` 只传"问题+检索文档"，**未使用 `state.messages`**（历史对话存了但 report 没读）→ 多轮追问时 LLM 看不到上文已说过的"30多家门店"，只能靠检索（还检索不到）→ 答"暂无"。

- **修改（agent/nodes.py）**：
  1. 新增 `_rewrite_query()`：一次 LLM 调用（`REWRITE_PROMPT`，max_tokens=300，JSON 输出）同时产出 **2 条改写 query**（疑问句→陈述式关键词句）+ **1 段 HyDE 假设答案**（60 字，可虚构，仅用于向量检索）；异常自动降级回原问题。
  2. `rag_node` kb 链路改**多路检索**：`[原问题 + 改写×2 + HyDE]` 各取 top_k=3 → `_dedup_docs` 按内容前缀去重取前 6；data 链路保持原问题检索（防经营结论稀释/延迟）。
  3. kb 链路 report 输入新增 `recent_history`（`state.messages` 最近 4 条 human/ai，截断 150 字）+ `KNOWLEDGE_REPORT_PROMPT` 第 2 条加"优先引用历史对话已确认信息"规则。

- **影响范围**：知识问答链路的检索与多轮记忆；经营分析链路不受影响。

- **验证**：
  - 单测改写：`公司有多少门店` → `门店数量 规模 直营与合作门店分布` + HyDE 带出"30多家"；`王志晓负责什么` → 保留人名；`上班时间几点` → 转考勤陈述。
  - 端到端（真实浏览器，**首次提问无历史**）：问"公司有多少门店" → 正确回答"30 多家直营和合作门店，分布北上杭苏常"，耗时 8.6s（含 rewrite 1-3s）。
  - 多轮追问（介绍公司 → 有多少门店）→ 正确引用上文"30多家"，并注明"这是上一轮提到的数据"。

### 2026-08-13 | 知识问答"答非所问 + 太慢"（36.6s）

- **现象/背景**：问"介绍一下公司"，回答变成入职须知（试用期/薪酬），且耗时 36.6s、入 tokens 4776。

- **根因（四点）**：
  1. **知识库缺料**：4 份文档（员工手册/晋升/薪资/话术）均无公司介绍 → 检索"介绍一下公司"全命中制度条款（score 0.53-0.55 低相关），模型拿无关内容硬凑。
  2. **kb 链路也检索了经营经验层**：`rag_node` 共用导致 kb 问题命中 2 条经营历史报告 → 污染答案 + 拖慢。
  3. `_build_report_input` 对 kb 也全量传 `query_result/analysis_result` 空壳字典（占 tokens）。
  4. kb 输出 max_tokens=3000 过大（正文限 350 字却给 3000，实际输出 1173 tokens）。

- **修改（agent/nodes.py）**：
  1. `rag_node`：`intent_type=="kb"` 跳过经验层检索。
  2. `_build_report_input`：kb 链路只传 question + retrieval_docs（450 字×5 条）。
  3. `report_node`：`is_knowledge` 时 max_tokens=3000→1200（data 保持 3000）。
  4. `KNOWLEDGE_REPORT_PROMPT` 规则 4：检索内容与问题**不相关**时如实说明"知识库暂无该类资料"+给指引，严禁无关硬凑。

- **验证**："介绍一下公司" 36.6s→**7.5s**；回答转为诚实说明缺料+指引。后续补《公司背景》等 3 文档入库后（见功能优化 8/13），回答有标准答案。

### 2026-08-13 | 回答"重复两遍"排查（非 bug）

- **现象/背景**：用户反馈"介绍一下公司"的回答重复出现两次。

- **排查结论**：**前后端均无重复**——接口实测单遍 423 字；真实浏览器实测单气泡 856 字无重复；`send()` 有 busy 锁；前端无历史消息渲染。用户看到的两遍 = 页面上 10:07/10:12/11:31 三次同样问题留下的**历史气泡**（内容几乎一样，未刷新页面时视觉像重复）。

- **验证**：刷新页面后单气泡正常。无需改代码。

### 2026-08-12 | 数据采集"登录态注入失败"——uvicorn stdout 重定向文件导致子进程崩溃

- **现象/背景**：前端点"智选展位/全部刷新"间歇失败，报"登录态注入失败（重试 3 次）"，且用户侧失败、我测试偶发成功。

- **根因（证据链：探针 + 对照实验）**：探针（最简 `python -c`）在 uvicorn 进程内也 rc=1 零输出 → **python 子进程本身启动即崩**。对照实验锁定差异：

  | 启动方式 | 子进程 |
  |---|---|
  | `nohup > 文件`（旧） | ❌ 启动即崩 |
  | Popen stdout=文件 | ❌ 崩溃 |
  | **管道方式（`\| tee`）** | ✅ 正常 |

  Windows 下 uvicorn 的 stdout 重定向到文件时，句柄继承导致其 subprocess 子进程（python/playwright）启动即失败（rc=1 零输出）→ 登录态注入必然失败。这是"我成功、用户失败"的真相（我测试时启动方式不同）。

- **修改**：
  1. 新增 `backend/start-backend.sh`：`python -m uvicorn ... 2>&1 | tee -a logs/server_uvicorn.log`（管道方式，后台+日志+子进程正常）；**以后必须用该脚本启动，禁止 nohup > 文件**。
  2. 顺带修复 `_kill_edge` 的 netstat `text=True` 编码 bug（GBK 输出遇非法 UTF-8 抛 UnicodeDecodeError → Edge 杀不掉）→ 改 bytes + errors=replace。

- **验证**：正式脚本启动 + 真实前端点击（playwright 打开页面点按钮）→ edge ✅ login ✅ campaign_download ✅ import ✅，连续多次稳定。

### 2026-08-12 | 数据采集失败根因链（playwright 线程限制 / WorkBuddy shim / Edge 累积假死）

- **现象/背景**：数据刷新按钮反复失败，报"Edge 未在线/登录态注入失败/智选展位请求异常"多种错误；用户质疑"独立脚本能跑，为什么按钮不行"。

- **根因（逐层排除后定位）**：
  1. **封装层多余**：按钮驱动是确定性执行，`refresh_market_data` 却穿着 LangChain `@tool` 外壳（历史遗留），已拆除改普通 async 函数。
  2. **playwright sync API 线程限制**：FastAPI `run_in_threadpool` 在线程池跑同步 playwright 报 `'PlaywrightContextManager' object has no attribute '_playwright'` → 注入改用 async playwright 在事件循环内执行。
  3. **WorkBuddy shim 环境污染**：uvicorn 进程环境被注入 `PYTHONPATH=...\vendor\shim`（含 sitecustomize.py），子进程 python 自动加载被劫持 → 子进程净化环境（剔除 PYTHONPATH/PYTHONHOME/CODEBUDDY_SESSION_ID 等）。
  4. **Edge 累积假死**：连续操作后 Edge CDP 探活正常但实际卡死（探活检测不到）→ 每次刷新强制重启 Edge（kill + 全新启动，profile 持久化不丢登录态）。
  5. **冷启动时序**：Edge 刚重启端口就绪但页面服务未 ready → `_ensure_edge` 等 CDP 探活 + sleep 6s 缓冲；注入重试间隔 2s→4s，重启后 sleep 8s。
  6. 附带修复：`download_zxz_report.py` 缺 `import sys`（NameError）+ `pages[0]` 固定取第一个 tab（多次刷新 tab 混乱）→ 按 URL 匹配页面。

- **验证**：真实前端连续多次点击全部稳定（edge ✅ login ✅ campaign_download ✅ import ✅）。历史教训记录：Windows 后台进程（uvicorn/nohup）子进程必须 CREATE_NO_WINDOW + bytes 解码；curl 测试用 `-m 600`。

### 2026-08-12 | 前端报 "Unexpected token 'I', \"Internal S\"... is not valid JSON"

- **现象/背景**：点数据采集按钮前端报 JSON 解析错误。

- **根因**：playwright driver（node 进程）启动失败的异常是**延迟抛出**的（在下一个 await 点才冒出）→ 逃逸出 `_ensure_login` → FastAPI 返回 500 纯文本 → 前端 `resp.json()` 解析失败。

- **修改（双端）**：
  1. 后端：`workflow_refresh` 全函数 try/except 兜底 → 任何异常返回 JSON `{"success":false,"error":...}`（HTTP 200），绝不裸 500；`_ensure_login` 整体 try 包裹 + 修 `asyncio` 未导入 bug。
  2. 前端：`resp.json()` 加 try/except，失败读 `resp.text()` 显示"HTTP 状态码+后端原文"。

- **验证**：模拟前端精确请求（同 URL/body/检查 HTTP 码）→ 200+JSON 全链路成功；异常路径 → 200 JSON 错误信息。

### 2026-08-11 | 报告正文空白 + 卡住（Supervisor 子图流式 bug）

- **现象/背景**：kb 问题（"请问绩效怎么算"）显示"✓约 4.5s"但报告正文空白、token=0；用户反馈项目卡住。

- **根因（双层）**：
  1. Supervisor+子图架构下 LangGraph 1.x 的 `stream_mode="messages"` **不透传子图内 report_node 的 stream chunk**（实测 token 事件数=0）。
  2. 新版 LangChain `AIMessage.content` 可能是 list of content blocks，`report_text += content` 抛异常被 except 吞掉。

- **修改**：
  1. `report_node`：`llm.invoke` → `llm.stream`（真流式，逐 chunk 收集 content）。
  2. `chat.py` 流式：`stream_mode="updates"`（节点级）+ final_report 增量 delta 按 6 字/帧字符分块模拟打字机（18ms/帧）。
  3. progress 文案按路由差异化："正在检索知识库…"（kb）/ "正在分析数据…"（data）。

- **验证**：kb 问题 → progress ×3 + token ×50 + done，usage 真实 4455 tokens。

### 2026-08-11 | 报告密集无分节 + KPI 卡与问题脱节

- **现象/背景**：数据类报告全堆在一段、不好阅读；KPI 卡固定 6 项（GMV/订单/客单价/推广/ROI/转化率）与问题无关（问团建套餐却显示推广 KPI）。

- **根因**：deepseek-v4-flash 长 prompt 下吞字严重（"关键指标"→"指标"、"建议"→"议"），但 `【】` 方括号包裹时 `】` 必现；前端 renderMarkdown 仅做行级严格匹配，模型输出 ✦/一段式无法识别。

- **修改**：
  1. `REPORT_SYSTEM_PROMPT` 大幅简化（去繁冗规则，3 条硬性要求），用 `【关键词】` 方括号包裹；跨 4 模型对比（deepseek-flash/kimi/glm/hy3）均稳定输出。
  2. 前端 `renderMarkdown` 重写：`SECTION_PATTERNS` 宽松正则（`【可省】必现` + 关键词核心字子串匹配），行级第一遍 + 段中关键词位置切分第二遍双重兜底。
  3. KPI 卡按问题关键词筛显：命中推广词只显推广三卡，否则只显 GMV/订单/客单价（chat.py done 数据加 `user_question`）。

- **验证**：模型实际输出 5 段均含 `】`，前端 5 个 section 全部识别成功。

---

## 功能优化

### 2026-08-20 | 多页工作台导航与可视化 RAG 知识库上传

- **现象/背景**：经营驾驶舱左侧导航原本使用页面锚点，点击后仅 URL 追加 `#chat` 等片段，不能体现独立功能模块；后端虽已有 `/api/rag/upload`，但没有用户可操作的上传入口、入库反馈或知识文档清单。
- **根因/修改**（`main.py`、`static/index.html`、`static/data.html`、`static/knowledge.html`、`static/sessions.html`、`static/workspace.css`、`api/chat.py`、`rag/loader.py`）：新增 `/data` 数据采集工作台、`/knowledge` 知识库管理页与 `/sessions` 会话记忆页，左侧工作区导航改为真实页面路由；会话页回跳智能对话时用 `?session=` 恢复原会话。知识库页支持 MD/TXT/PDF/DOCX 拖放或选择上传、Token 鉴权、进度/错误反馈、已入库文件清单；新增 `GET /api/rag/documents` 返回上传规则和切割策略。上传处理移入线程池，避免解析/向量化阻塞 SSE 对话。复用现有安全校验和父子切割：章节感知父块 1200/150、检索子块 350/50，重复同名文件仅覆盖自身旧向量，上传文件由 `rag_uploads` Docker 数据卷持久化。
- **影响范围**：前端工作区导航、知识库运维体验与上传时的服务并发性；原聊天、RAG 检索、数据采集 API 与向量结构保持兼容。
- **验证/决策**：待 Docker 重建后手动验证 `/knowledge` 上传一份测试文档并确认返回 chunk 数和知识问答命中；页面内嵌 JS 与 Python 语法级校验通过后再提交。

### 2026-08-20 | Agent 能力入口可用化与 README 运行预览

- **现象/背景**：左侧 `AGENT CAPABILITIES` 下的“数据分析、混合检索、行动建议”原先是非交互说明文字，用户点击无页面跳转；README 缺少直观运行截图，阅读者无法快速了解产品界面。
- **根因/修改**：将三项能力分别链接到现有真实页面：数据分析 → `/data` 数据采集工作台，混合检索 → `/knowledge` RAG 知识库管理，行动建议 → `/` 智能对话；所有工作台页面保持一致的可用能力入口。README 增加产品预览图和多页入口说明。
- **影响范围**：仅导航可用性与项目文档展示，不新增重复业务逻辑。
- **验证/决策**：导航链接与静态页面路由在 Docker 重建后验证；运行截图作为仓库图片资源提交，README 使用相对路径引用，GitHub 页面可直接渲染。

### 2026-08-20 | 前端经营驾驶舱视觉重构与新会话欢迎页修复

- **现象/背景**：原页面以基础聊天两栏布局为主，功能入口、当前工作上下文和 Agent 能力边界不够清晰，项目展示的产品化完成度偏弱；“新对话”先清空 DOM 再查找欢迎节点，导致欢迎语无法恢复。
- **根因/修改**（`static/index.html`）：在不改动后端接口、SSE、数据采集、模型切换和历史会话逻辑的前提下，新增深色工作台导航、工作区顶栏、数据工作流栏、对话区标题、经营 Copilot 欢迎卡及优化后的历史会话栏；保留全部既有 DOM id 与 `ask/runWorkflow/send` 事件。`clearChat()` 改为先克隆欢迎节点再清空聊天容器，保证新会话仍能显示快捷问题入口。
- **影响范围**：仅前端展示与新建会话的欢迎界面；数据查询、知识检索、模型通道与会话 API 不变。
- **验证/决策**：HTML 解析与内嵌 JavaScript 语法校验通过；本机系统 Python 缺少项目依赖 `langchain_core`，pytest 未能收集，需在 Docker 容器或项目虚拟环境中执行完整测试。重新构建 `app` 容器后通过 `http://localhost` 验收页面和对话功能。

### 2026-08-19 | OpenAI 通道支持自定义 base_url（第三方兼容端点接入）

- **现象/背景**：`openai` 通道此前只连官方 `api.openai.com`，商汤 SenseNova 等第三方 OpenAI 兼容端点无法接入；若填了第三方 key 又未配置 base_url，ChatOpenAI 默认连官方地址 → key 无效 → 知识检索/报告全部失败。
- **根因/修改**（`config/settings.py`、`config/llm_factory.py`、`.env.example`）：新增 `openai_base_url`（env `BIZ_OPENAI_BASE_URL`），`_build_llm` 的 openai 分支透传 `base_url`，留空回退官方；`.env.example` 补充注释。
- **影响范围**：openai 通道可接任意 OpenAI 兼容端点；deepseek/codebuddy/local 通道不受影响。
- **验证**：商汤 SenseNova 端点配置后对话/检索正常。

### 2026-08-19 | 后端直接运行入口 + start-all.sh 一键启动完善

- **现象/背景**：① 后端只能靠 uvicorn 命令行启动，VSCode F5 / 右键 Run Python File 无法直接运行；② `start-all.sh` 读取 .env 未剥离行尾 `# 注释`，且硬编码个人 Python 路径，换机器不可用。
- **根因/修改**：
  1. `main.py` 增加 `if __name__ == "__main__"` 入口（`uvicorn.run(app, host=127.0.0.1, port=8000)`）；
  2. `start-all.sh`：`.env` 解析加 `strip_comment`（去行尾注释/空格）；Python 探测 `.venv/Scripts/python.exe → .venv/bin/python → PATH python`；`WB2API_DIR` 可被环境变量覆盖；顶部补使用备忘（一键启动/status/stop/分步启动/验证）。
- **影响范围**：开发启动体验与脚本可移植性。
- **验证**：`bash -n` 通过；一键 `./start-all.sh` 按 .env 自动拉起 8788 + 8000。

### 2026-08-14 | LLM 运行时切换 + 会话删除/导出 + 依赖锁定 + trace 修正（#15）

- **背景**：模型切换靠改 .env + 重启；会话不能删除/导出；requirements 无锁文件；`_build_trace` 对 kb 问题硬编码"数据查询：GMV=…"。
- **修改**：
  1. `config/llm_factory.py`：`set_active_provider/get_active_provider/list_providers`（内存级切换 + 切换即校验 Key + 实例缓存）；`api/chat.py` 新增 `GET /api/llm/providers`、`POST /api/llm/switch`；前端 header 加 LLM 下拉（实时切换并回显）。
  2. 会话管理：`DELETE /api/sessions/{id}` + 前端列表删除按钮（confirm 二次确认）；前端「导出」按钮下载当前会话 txt。
  3. 依赖锁定：`uv pip compile` 生成 `requirements.lock`（458 行）；`requirements-dev.txt`（pytest/ruff）。
  4. `_build_trace`：按 `intent_type` 推导（kb→"知识问答链路：未调用数据工具"；data→GMV 或真实工具名）。
- **验证**：`/api/llm/switch` 实测 deepseek↔codebuddy 切换成功（切换失败有明确错误）；删除/导出前端逻辑已实现；`uv pip compile` 无错误。

### 2026-08-14 | pytest 单测 + GitHub Actions CI（#13）

- **背景**：仅 selftest_security.py（23 用例），无单测与 CI。
- **修改**：新增 `tests/`（conftest 锚定根目录）——① `test_routing.py` 路由矩阵（含双列表冲突用例）；② `test_analysis.py` 指标边界（除零/空窗口/无数据）；③ `test_tools_contract.py` 工具真实返回结构快照（MySQL 未连通自动跳过）；④ `test_rag_eval.py` golden set 命中（向量库未就绪跳过）；`pytest.ini`；`.github/workflows/ci.yml`（MySQL service + ruff + py_compile + pytest）。
- **验证**：本机 **36 passed**。

### 2026-08-14 | RAG 检索评测集与脚本（#11）

- **背景**：Query Rewrite + HyDE 是"检索不到就靠改写硬捞"，无 golden test set 与检索评估，改参数是否变好全靠感觉。
- **修改**：新增 `data/eval/golden_set.json`（21 条问题→预期文档，覆盖公司背景/高管/日常工作/员工手册/晋升/薪资/话术）+ `scripts/eval_rag.py`（Hit@k、首命中排名、失败用例明细；命中率 <50% 退出码 1 防回归）。
- **验证**：本机 Hit@5 = **90.5%**（19/21）；README/docs 标注"改 RAG 参数后必须跑"。

### 2026-08-13 | Query Rewrite + HyDE 检索增强

- **背景**：疑问句（"公司有多少门店"）与文档陈述句（"全国拥有30多家..."）存在语义鸿沟，小 embedding + 词频 BM25 均抓不住"数量/规模"意图。
- **修改（agent/nodes.py）**：新增 `_rewrite_query()`——一次 LLM 调用输出 2 条改写 query + 1 段 HyDE 模拟答案；`rag_node` kb 链路多路检索 `[原问题+改写+HyDE]` 去重取前 6；失败自动降级原问题。
- **验证**：首问"公司有多少门店"（无历史）直接命中"30多家直营和合作门店"，8.6s；改写质量单测通过（保人名/转陈述）。

### 2026-08-13 | 会话管理功能（新对话 / 历史列表 / 继续对话）

- **背景**：用户要求仿大模型聊天应用——开启新对话、右侧历史对话列表、点击查看/继续对话。
- **修改**：
  - 后端（api/chat.py）：新增 `GET /api/sessions/{session_id}/messages`（取单会话历史消息）；复用 `GET /api/sessions`（列表）、`POST /api/sessions/{id}`（幂等创建）、`_load/_save_session_history`（按 session 存取，窗口 MAX_HISTORY_TURNS=6）。
  - 前端（static/index.html）：两栏布局（`.app` flex + 右侧 `.sidebar` 280px：＋新对话按钮 + `#sessionList`）；JS：`currentSessionId`（可切换，localStorage 持久化）、`newConversation()`、`loadSessions()`/`renderSessions()`（标题=last_question/条数/时间/高亮）、`openSession(sid)`（渲染历史+设当前会话）；send 完成后刷新列表。
  - 记忆上下文：会话 id 即记忆 key——新对话=空上下文；继续对话=后端按 session_id 恢复最近 12 条作 LLM 上下文 + 前端渲染历史；各会话互不串扰。
- **验证**（真实浏览器）：新对话×2 → 列表更新 → 切回会话1（历史渲染 ✓）→ 继续追问（上下文记忆生效，回答接着话题不重复介绍）→ 高亮更新 ✓ → JS 零报错。

### 2026-08-13 | 远程账号 auth 接入（多账号并存，不再本机登录）

- **背景**：workbuddy2api 用本机登录态，用户想用其他账号的 CodeBuddy 积分。
- **关键调研结论**：WorkBuddy 应用账号（wb_at_ 前缀 token，workbuddy-auth 体系）与 CodeBuddy（copilot.tencent.com，RS256 无前缀）是**两个独立产品线，积分池不互通**——第一次导出的 WorkBuddy 应用 auth 实测 401 不可用；第二次从 `CodeBuddyExtension` 目录导出的 auth 完全兼容。
- **修改**：新增 `D:\workbuddy2api\start-wb2api-remote.sh`（端口 8788 + `CODEBUDDY_AUTH_DIR=D:/workbuddy2api/auth_remote`，注意必须 Windows 风格路径）；`backend/.env` 的 `BIZ_CODEBUDDY_BASE_URL` 指向 8788（标注 8788=远程账号 / 8787=本机账号，随时可切回）。
- **验证**：8788 health 显示新账号（18657112521）token 未过期；真实请求 HTTP 200 + `usage.credit=0.01`（确认走新账号积分池）；项目全链路可用。

### 2026-08-13 | 三个新知识文档入库（公司背景/日常工作/高管名单）

- **背景**：知识库缺公司介绍/日常安排/组织架构材料（"介绍一下公司"无标准答案）。
- **修改**：`rag/loader.py` `DOC_TYPE_MAP` 新增 company/daily/org 三映射；文档拷入 `rag/data/`，ingest 入库 5 chunks（表格文档 17行×4列 → 每行转 `序号 | 任务名称 | 负责人 | 工作要求` 文本，父子切割按行切）。
- **验证**：三类问题（公司介绍/日常工作/王志晓）均正确命中新文档。

### 2026-08-13 | 知识层检索排除历史经营报告（检索纯净）

- **背景**："王志晓负责什么"命中 2 条 auto-generated 历史经营报告（score 同值 0.514，稀释相关性）。
- **修改**：`ChromaClient.query` 加 `exclude_types` 参数（`$ne` where）；`search_operation_knowledge` 排除 `report` 类型（经验层由 rag_node 单独查）。
- **验证**：知识问答检索不再混入历史报告。

### 2026-08-12 | 数据采集全自动化（Edge 自动拉起 + 登录态自动注入）

- **背景**：点"全部刷新"应自动完成，而非报"Edge 未在线"让用户手动开浏览器。
- **修改**（tools/data_ingest_tool.py）：`_ensure_edge`（9222 不在线自动启动 Edge + 持久化 profile）、`_ensure_login`（自动探测 cookies.json → import_login_state 注入，3 次重试）、campaign 下载 2 次重试；拆掉 LangChain `@tool` 包装（按钮驱动无需工具外壳）。
- **验证**：真实前端点击"全部刷新"= edge ✅ login ✅ campaign_download ✅ import ✅（traffic/transaction/consult 返回手动指引——美团后台 SPA 限制，脚本无法自动导航）。

### 2026-08-12 | LLM 双通道（DeepSeek 直连 / CodeBuddy workbuddy2api）

- **背景**：复用 CodeBuddy 账号额度作为可选 LLM 通道；codebuddy-proxy（19090，Docker）实测**不支持 tool calling**（无法驱动 LangGraph ToolNode）。
- **修改**：`config/llm_factory.py` 新增 `provider=="codebuddy"` 分支（ChatOpenAI → workbuddy2api 127.0.0.1:8787/v1，支持原生 tool calling）；`.env` 加 `BIZ_CODEBUDDY_*`；`BIZ_LLM_PROVIDER` 一行切换。
- **验证**：tool_calls 正常返回（get_sales → store_id=1）；8/13 起项目切到远程账号实例（8788）。
- **文档/部署包**：`docs/07_LLM通道部署指南.md` + `workbuddy2api通道部署包_20260812.zip`（服务源码净化版，可外发部署）。

### 2026-08-11 | Supervisor 双 Agent 架构（经营分析 / 知识问答）

- **背景**：单 Agent 数据分析和内部知识问答重合，需职责拆分。
- **修改**（LangGraph 1.x subgraph）：
  - `agent/data_agent.py`：经营分析子图（intent→tools回环→analysis→rag→report）
  - `agent/kb_agent.py`：知识问答子图（rag→report，纯 RAG 无工具无分析）
  - `agent/graph.py`：Supervisor 主图（`is_data_question` 确定性路由 → 条件边分发子图）
  - `state.py`：+`intent_type` 路由标记；API 层零改动（build_graph 出口保持）
- **验证**：kb 问题 → 知识 Agent 口语化回答；data 问题 → 经营 Agent 真实数据诊断。

### 2026-08-11 | 前端示例问题扩充 2→8 个

- **修改**：`static/index.html` 示例问题分两组（📊 经营分析 4 + 📖 内部制度 4），带 suggest-tag 分组样式。

---

## 已知问题 / 待优化

### 2026-08-14 | deepseek-v4-flash thinking mode 不支持 with_structured_output 的 tool_choice

- **问题描述**：`with_structured_output`（function calling 模式）会强制 tool_choice，deepseek-v4-flash 在 thinking 模式下返回 400 `Thinking mode does not support this tool_choice`。
- **当前处理**：已内置三级降级（with_structured_output → prompt JSON 手动解析 → 流式 markdown），线上走方案二，报告质量一致；无需修复。如换用支持 tool_choice 的模型（glm-5.2 等）可自动走方案一。

### 2026-08-14 | ChatDeepSeek 流式不返回 usage（token 用量为估算）

- **问题描述**：deepseek 流式响应不带 token_usage，`on_chat_model_end` 采集不到 → usage 按报告字数估算并标注 `estimated: true`。
- **建议**：如需精确用量，改用非流式接口或等 DeepSeek 流式 usage 支持；当前估算仅供展示。

### 2026-08-14 | 向量库重建后历史报告经验层清空

- **问题描述**：本次为启用 bge-zh(512) + 父块独立存储删除 `chroma_db/` 重建，旧 auto-generated 历史报告（经验层）随之清空；后续每次对话生成的报告会重新累积。
- **建议**：无数据丢失风险（运行时产物，30 天有效期设计）；如需保留可导出旧报告文档重新 ingest。

### 2026-08-13 | deepseek-v4-flash 偶发吞字（kb 链路）

- **问题描述**：模型在长输出时偶发吞字（如"《员工手册》"→"《工手册》"）。data 链路已由 #14 结构化输出根治；**kb 链路仍为口语化 markdown**，个别位置仍可能偶现。
- **建议**：来源引用处强化完整书名；或 kb 链路换 glm-5.2（吞字少）。

### 2026-08-12 | 客流/交易/咨询三模块无法全自动

- **问题描述**：美团商家后台 SPA 反爬限制（菜单 React 合成事件点击无效、menuId 动态过期、iframe 直访被拦）——脚本无法自动导航，需用户手动打开对应 tab 后重刷（Edge 与登录态已自动就绪）。
- **当前状态**：未解决（平台技术封锁）。半自动体验已优化。

---

## 维护说明

**如何追加新条目**：
- 每次修复 bug → 追加到「已修复问题」章节顶部
- 每次优化功能 → 追加到「功能优化」章节顶部
- 发现但未修复的问题 → 追加到「已知问题 / 待优化」章节顶部
- 时间倒序排列（最新在上）
- 每个条目包含：日期 | 标题、现象/背景、根因/修改、影响范围、验证/决策

**条目模板**：
```markdown
### YYYY-MM-DD | 简短标题

- **现象/背景**：...
- **根因/修改**：...
- **影响范围**：...
- **验证/决策**：...
```
