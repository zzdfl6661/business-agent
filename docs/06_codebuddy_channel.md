# 06_CodeBuddy 通道接入说明

> 日期：2026-08-11
> 功能：为 Business Agent 新增一条**可选的** LLM 通道（CodeBuddy/WorkBuddy 账号额度），与 DeepSeek 直连并存、随时切换。

## 1. 背景与结论

| 项 | 说明 |
|---|---|
| 需求 | 复用 CodeBuddy 账号额度，作为 Agent 的可选 LLM 通道，**不覆盖 DeepSeek 配置** |
| 方案 | `workbuddy2api`（GitHub: ShouZhuo0413/codebuddy2openai）——读取本机 WorkBuddy 登录态，直连腾讯后端，**支持原生 tool calling** |
| 为什么不用 codebuddy-proxy | codebuddy-proxy（cnb.cool/cloud-mt）**不支持 tool calling**（CLI/HTTP 后端均实测），无法驱动 LangGraph ToolNode；仅适合纯聊天客户端 |
| 验证结论 | ✅ workbuddy2api 返回标准 OpenAI `tool_calls`（实测 get_sales → store_id=1），LangChain `bind_tools` 解析正常 |

## 2. 架构

```
Business Agent (LangGraph)
   ├─ 通道A（默认）: ChatDeepSeek ──► https://api.deepseek.com        （DeepSeek 官方）
   └─ 通道B（可选）: ChatOpenAI ──► http://127.0.0.1:8787/v1          （workbuddy2api 本地代理）
                                         └─► 本机登录态 workbuddy-desktop.info ──► 腾讯 CodeBuddy 后端
```

## 3. 服务部署（已就绪）

- 代码：`D:\workbuddy2api`（独立 venv：`D:\workbuddy2api\.venv`，依赖 fastapi/uvicorn/httpx）
- 服务：监听 `127.0.0.1:8787`，直连腾讯后端（账号「骷髅小兵」，token 未过期）
- 管理脚本（Git Bash）：
  ```bash
  cd /d/workbuddy2api
  ./start-wb2api.sh start|status|stop|restart|logs
  ```
- 登录态失效处理：WorkBuddy 桌面端重新登录即可；token 过期时 `/health` 会显示 `token_expired: true`
- 可用模型（`GET /v1/models` 实测）：`glm-5.2 / glm-5.1 / glm-5v-turbo / kimi-k2.7 / kimi-k2.6 / kimi-k2.5 / deepseek-v4-pro / deepseek-v4-flash / minimax-m3-pay / hy3-preview-agent / auto`

## 4. 项目侧改动（已完成）

| 文件 | 改动 |
|---|---|
| `config/settings.py` | 新增 `codebuddy_base_url` / `codebuddy_model` / `codebuddy_api_key` 三项 |
| `config/llm_factory.py` | 新增 `provider == "codebuddy"` 分支（ChatOpenAI 指向 workbuddy2api，支持原生 tool calling；按 provider 缓存复用） |
| `.env` | 追加 `BIZ_CODEBUDDY_*` 配置（**DeepSeek 相关行未动**） |
| `.env.example` | 同步示例 |

## 5. 切换通道

改 `.env` 一行后重启后端服务（根目录 `uvicorn main:app --reload --port 8000`）；或**无需重启**：页面右上角 LLM 下拉实时切换（`POST /api/llm/switch`）：

```ini
# 用 CodeBuddy 通道：
BIZ_LLM_PROVIDER=codebuddy

# 恢复 DeepSeek 通道（默认）：
BIZ_LLM_PROVIDER=deepseek
```

可选模型（`BIZ_CODEBUDDY_MODEL`）：

```ini
BIZ_CODEBUDDY_MODEL=glm-5.2          # 推荐（支持 tool calling，综合最佳）
BIZ_CODEBUDDY_MODEL=deepseek-v4-flash # 与 DeepSeek 通道同款模型，对比用
BIZ_CODEBUDDY_MODEL=hy3-preview-agent # 推理增强
```

## 6. 已知注意点

- **无 API Key 认证**：workbuddy2api 默认不校验 Key（复用本机登录态），`codebuddy_api_key` 留空即可；如需鉴权用 `converter.py --api-key xxx` 启动
- **依赖本机登录态**：仅本机可用，换机器需在对应机器部署 workbuddy2api 与 WorkBuddy 登录
- **token 过期**：个人中心或桌面端重新登录；`/health` 的 `token_expired` 字段可监控
- **编码**：Windows 下 curl 直接传中文 JSON 会因 GBK 乱码，测试用 `--data-binary @file`（UTF-8 文件）
- **credit 计费**：响应中 `usage.credit` 为本次消耗的 CodeBuddy 积分，可在个人中心查看明细
