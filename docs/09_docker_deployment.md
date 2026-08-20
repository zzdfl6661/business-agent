# Docker 生产验证部署

本项目首版 Linux 部署包含 `app + mysql + nginx` 三个 Compose 服务。
Chroma 是 app 进程内向量库，通过 `chroma_data` 卷持久化；不单独启动 Redis、Milvus、Edge 或 CodeBuddy。

## 1. 准备配置

```bash
cp deploy/.env.production.example .env
```

生产环境至少填写：

```ini
BIZ_LLM_PROVIDER=deepseek
BIZ_DEEPSEEK_API_KEY=...
BIZ_DB_USER=root
BIZ_DB_PASSWORD=...
BIZ_API_TOKEN=...
BIZ_EMBEDDING_PROVIDER=fastembed_bge_zh
```

如果使用第三方 OpenAI-compatible 模型：

```ini
BIZ_LLM_PROVIDER=openai_compatible
BIZ_OPENAI_COMPATIBLE_API_KEY=...
BIZ_OPENAI_COMPATIBLE_BASE_URL=https://provider.example/v1
BIZ_OPENAI_COMPATIBLE_MODEL=...
```

Compose 会将容器内数据库地址覆盖为 `mysql`，门店文件和 Chroma 路径分别固定为 `/app/data/stores.json` 与 `/app/chroma_db`。

## 2. 启动与检查

服务器安装 Docker Compose v1 时：

```bash
docker-compose build
docker-compose up -d
docker-compose ps
docker-compose logs -f app
curl http://127.0.0.1/health
```

Compose v2 使用同样命令，将 `docker-compose` 替换为 `docker compose`。

首次启动会下载依赖和 embedding 模型，并从 `rag/data/` 入库；该过程可能较慢，完成后查询会复用持久化缓存。

## 3. 数据与备份

- `mysql_data`：业务库、会话、审计；上线前导入真实 MySQL 备份。
- `chroma_data`：知识库索引；删除该卷会触发重新向量化。
- `rag_uploads`：用户上传知识文档。
- `fastembed_cache` / `hf_cache`：Embedding 模型缓存。

生产服务器不要挂载 Windows `data/edge_debug_profile`，也不要在 Linux 容器执行 Edge/Playwright 数据采集。

## 4. 网络边界

公网仅开放 80/443；8000、3306、9222、8787、8788 不对公网开放。当前 Nginx 配置先提供 HTTP 反代，正式上线前应在 Nginx 或云负载均衡层配置 TLS。
