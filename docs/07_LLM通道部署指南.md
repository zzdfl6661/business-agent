# Business Agent — CodeBuddy LLM 通道（workbuddy2api）部署指南

> 版本：v1.0 ｜ 日期：2026-08-12 ｜ 适用：需要把「WorkBuddy/CodeBuddy 桌面端账号额度」接入 AI 应用，且要求**支持工具调用（tool calling）**的场景。

---

## 0. 这份文档解决什么问题

「企业经营智能决策 Agent」系统（下称 **Business Agent**）是一个用 LangGraph 编排的多 Agent 应用，它需要一个大语言模型（LLM）来做意图识别、数据分析和报告生成，并且**必须支持工具调用**（模型要能返回 `tool_calls`，Agent 才能去查询数据库、调用工具，再继续生成回答）。

LLM 有两个可选来源：

| 通道 | 说明 | 工具调用 |
|---|---|---|
| DeepSeek 官方 API | 买 DeepSeek 的 Key，直连 `api.deepseek.com` | ✅ |
| **CodeBuddy 账号额度**（本文主角） | 复用你电脑上已登录的 WorkBuddy/CodeBuddy 桌面端账号，**不花钱买 Key** | ✅（经 workbuddy2api 转换） |

本文讲的就是第二条路：**如何在另一台机器上把 CodeBuddy 账号额度变成 OpenAI 兼容的、支持工具调用的本地 LLM 服务**。

> ⚠️ 曾尝试过的替代方案 `codebuddy-proxy`（Docker 版，端口 19090）：虽然也能转成 OpenAI 兼容接口，但**实测不支持 tool calling**（模型响应里没有标准 `tool_calls` 字段），无法驱动 LangGraph 的工具回环，只适合纯聊天。**不要用它做 Agent 的 LLM 通道。**

---

## 1. 原理：workbuddy2api 是怎么让"不能调工具"变成"能调工具"的

```
┌─────────────────────┐         ┌──────────────────────────────┐         ┌───────────────────────┐
│  Business Agent     │  HTTP   │  workbuddy2api（本地代理）    │  HTTP   │  腾讯 CodeBuddy 后端   │
│  (LangGraph)        │ ──────► │  127.0.0.1:8787/v1           │ ──────► │  （云端模型服务）      │
│  ChatOpenAI(base    │ ◄────── │  ▲ 读取本机 WorkBuddy 登录态   │ ◄────── │                       │
│   _url=:8787/v1)    │  OpenAI │  │  auth/workbuddy-desktop    │  流式   │                       │
└─────────────────────┘  兼容    └──┼───────────────────────────┘         └───────────────────────┘
                                    │
                      ┌─────────────┴──────────────┐
                      │ 关键能力：tool calling 翻译   │
                      │  OpenAI tool_calls 请求  ──►│
                      │  翻译成 CodeBuddy 格式       │
                      │  返回再还原成标准 tool_calls  │
                      └────────────────────────────┘
```

**三个关键事实：**

1. **不用买 Key、不用填密钥**：workbuddy2api 直接读取本机 WorkBuddy 桌面端的登录态文件（`auth/workbuddy-desktop.info`），拿你的账号额度去调用腾讯 CodeBuddy 云端模型。所以部署前提是**这台机器上必须装有 WorkBuddy 桌面端且已登录**。
2. **工具调用靠"翻译层"实现**：workbuddy2api 在中间做双向协议翻译——把你的 OpenAI 格式 `tool_calls` 请求转成腾讯后端认识的格式，再把响应还原成标准 `tool_calls`。因此下游应用（LangChain / LangGraph / OpenAI SDK）**完全无感**，跟用 OpenAI 官方 API 一样。
3. **端口固定 8787**：服务监听 `http://127.0.0.1:8787/v1`，OpenAI 兼容，支持流式（SSE）。

---

## 2. 部署前置条件（目标机器）

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10/11（本文按 Windows 写；Linux 需改 venv 路径） |
| 终端 | Git Bash（用于执行 `start-wb2api.sh` 脚本） |
| Python | 3.10+（建议 3.12） |
| 桌面端 | **已安装并登录 WorkBuddy 桌面端**（登录态是服务的"钥匙"） |
| 端口 | 8787 未被占用 |

---

## 3. 部署步骤（约 5 分钟）

### 3.1 解压并准备环境

把压缩包解压到任意目录（本文以 `D:\workbuddy2api` 为例），打开 Git Bash：

```bash
cd /d/workbuddy2api

# 1) 创建独立虚拟环境
python -m venv .venv

# 2) 安装依赖（fastapi / uvicorn / httpx）
.venv/Scripts/python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3.2 启动服务

```bash
./start-wb2api.sh start
```

看到以下输出即成功：

```
[OK] workbuddy2api 已启动，监听 http://127.0.0.1:8787/v1（原生 tool calling）
```

其他管理命令：

```bash
./start-wb2api.sh status   # 查看运行状态 + 登录态是否过期
./start-wb2api.sh stop     # 停止
./start-wb2api.sh restart  # 重启
./start-wb2api.sh logs     # 实时查看日志
```

### 3.3 验证服务

```bash
# ① 健康检查（注意 token_expired 字段）
curl http://127.0.0.1:8787/health

# ② 可用模型列表
curl http://127.0.0.1:8787/v1/models
```

`/health` 返回示例：

```json
{"status":"ok","platform":"win32","python":"3.13.14","auth_file":"...\\workbuddy-de..."}
```

> 🔑 如果看到 `token_expired: true` 或 health 不是 ok：打开 WorkBuddy 桌面端重新登录一次即可，无需重启服务（它会自动读到新登录态）。

### 3.4 验证「工具调用」真的可用（关键！）

用一条带 `tools` 参数的请求测试（这是 Agent 能跑起来的前提）：

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{
  "model": "glm-5.2",
  "messages": [{"role": "user", "content": "查询1号门店的销售额"}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_sales",
      "description": "查询门店销售额",
      "parameters": {
        "type": "object",
        "properties": {"store_id": {"type": "integer"}},
        "required": ["store_id"]
      }
    }
  }]
}
EOF
```

**成功的标志**：响应里 `choices[0].message.tool_calls` 不为空，能看到 `"function": {"name": "get_sales", "arguments": "{\"store_id\": 1}"}`。

> 💡 Windows 下 curl 直接传中文会乱码（GBK 编码问题），务必用 `--data-binary @文件`（UTF-8 文件）或上面这种 here-doc 方式。

---

## 4. 接入 Business Agent 项目（本项目已在用，供参考/移植）

### 4.1 依赖

Business Agent 后端通过 **LangChain 的 `ChatOpenAI`**（`langchain-openai` 包）连接 workbuddy2api——OpenAI 兼容客户端天然支持 `bind_tools`，无需额外依赖。

### 4.2 LLM 工厂代码（`config/llm_factory.py`，项目根目录）

```python
if provider == "codebuddy":
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.codebuddy_model,
        api_key=settings.codebuddy_api_key or "codebuddy-local",  # workbuddy2api 默认不校验 Key
        base_url=settings.codebuddy_base_url,                      # http://127.0.0.1:8788/v1（远程账号）/ 8787（本机）
        temperature=settings.llm_temperature,
    )
```

> 当前 `.env` 的 `BIZ_CODEBUDDY_BASE_URL` 指向 **8788**（远程账号 18657112521 的 workbuddy2api 实例）。

### 4.3 配置项（`.env`，项目根目录）

```ini
# ---- LLM 通道切换 ----
BIZ_LLM_PROVIDER=codebuddy        # deepseek（默认直连） / codebuddy（走 workbuddy2api）

# ---- CodeBuddy 通道 ----
BIZ_CODEBUDDY_BASE_URL=http://127.0.0.1:8788/v1   # 8788=远程账号 ｜ 8787=本机账号
BIZ_CODEBUDDY_MODEL=deepseek-v4-flash   # 可用模型见下表
BIZ_CODEBUDDY_API_KEY=                  # workbuddy2api 默认不校验，留空即可
```

**切换通道 = 改 `BIZ_LLM_PROVIDER` 一行后重启后端；或页面右上角 LLM 下拉实时切换（`POST /api/llm/switch`，无需重启）。**

### 4.4 可用模型（`GET /v1/models` 实测）

```
glm-5.2 / glm-5.1 / glm-5v-turbo / kimi-k2.7 / kimi-k2.6 / kimi-k2.5
deepseek-v4-pro / deepseek-v4-flash / minimax-m3-pay / hy3-preview-agent / auto
```

建议：综合能力用 `glm-5.2`；与 DeepSeek 通道对比用 `deepseek-v4-flash`；需要推理增强用 `hy3-preview-agent`。

### 4.5 后端启动顺序（Business Agent 完整链路）

```bash
# 1) 先起 LLM 通道（8787）
cd /d/workbuddy2api && ./start-wb2api.sh start

# 2) 再起业务后端（8000，务必用管道方式，见项目 README；项目在根目录，无 backend/ 子目录）
cd "/d/agent learning/Business  Agent" && ./start-backend.sh
```

---

## 5. 常见问题排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `/health` 返回 `token_expired: true` | WorkBuddy 登录态过期 | 打开 WorkBuddy 桌面端重新登录 |
| 换了一台机器，服务起不来 | 登录态是本机的 | 新机器装 WorkBuddy 并登录后再部署 |
| Agent 报"模型没有返回 tool_calls" | 用的可能是 codebuddy-proxy（19090） | 确认 base_url 是 `127.0.0.1:8787/v1`，不是 19090 |
| curl 传中文请求乱码 | Windows GBK 编码 | 用 `--data-binary @utf8文件` 或 here-doc |
| 计费疑问 | 消耗 CodeBuddy 账号积分 | 响应 `usage.credit` 字段看单次消耗；个人中心看明细 |
| 端口被占用 | 其他程序占 8787 | 改 `start-wb2api.sh` 里的 `PORT` 变量（需同步改项目 base_url） |

---

## 6. 安全与注意

- **无 Key 认证**：workbuddy2api 默认不校验 API Key（`codebuddy_api_key` 留空）。它只监听 `127.0.0.1`，本机可用；不要暴露到公网。如需鉴权可用 `converter.py --api-key xxx` 启动。
- **登录态即凭证**：本机登录态文件相当于你的账号凭证，不要把它拷贝外传。
- **仅本机可用**：服务与 WorkBuddy 登录态绑定，不能跨机器直接迁移。

---

*本指南配套压缩包包含：本文档 + workbuddy2api 服务源码（净化版，含 Docker 备用部署方式）。*
