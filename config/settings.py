"""
统一配置中心（pydantic-settings）
================================
- 环境变量前缀：BIZ_（如 BIZ_LLM_PROVIDER / BIZ_DB_HOST）
- 读取项目根目录 .env；全部配置集中于此，业务代码通过 settings 单例访问
- 仅支持真实数据链路（MySQL + 真实 LLM），无任何 Mock/模拟数据
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（.env 所在位置；config/ 位于根目录下）
PROJECT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BIZ_",
        env_file=PROJECT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- LLM ----------
    llm_provider: str = "deepseek"            # deepseek / openai_compatible / openai(alias) / local / codebuddy
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"  # 兼容旧模型名 deepseek-chat，可 env 覆盖
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # OpenAI 兼容端点 base_url（可指向商汤 SenseNova / vLLM / 中转等；
    # 留空 = 官方 api.openai.com）。注意：商汤等第三方端点必须显式配置，
    # 否则 ChatOpenAI 默认连官方地址，key 无效 → LLM 全部失败
    openai_base_url: str = ""
    # 推荐的新命名；为空时兼容读取 BIZ_OPENAI_* 旧配置。
    openai_compatible_api_key: str = ""
    openai_compatible_model: str = ""
    openai_compatible_base_url: str = ""
    local_base_url: str = "http://localhost:11434/v1"
    local_model: str = "qwen2.5:14b"
    # CodeBuddy 通道（workbuddy2api 本地代理，支持原生 tool calling；与 DeepSeek 通道并存可切换）
    codebuddy_base_url: str = "http://127.0.0.1:8787/v1"
    codebuddy_model: str = "glm-5.2"          # 可用: glm-5.2 / glm-5.1 / kimi-k2.7 / deepseek-v4-flash / hy3-preview-agent 等
    codebuddy_api_key: str = ""               # workbuddy2api 默认不校验，留空即可
    llm_temperature: float = 0.3

    # ---------- MySQL ----------
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = "root"
    db_name: str = "business_agent"

    # 门店主数据 JSON（美团后台门店列表，含 search_keyword/budget_keyword）
    stores_json: str = ""

    # ---------- RAG ----------
    vector_store_type: str = "chroma"          # chroma / milvus
    # 向量库目录（默认锚定项目根目录，避免依赖启动时工作目录）
    chroma_dir: str = str(PROJECT_DIR / "chroma_db")
    embedding_provider: str = "chroma_default"  # chroma_default / fastembed_bge_zh / openai

    # ---------- 安全（API 鉴权） ----------
    # 设置后所有非公开接口（/api/*、/docs 等）必须携带
    #   Authorization: Bearer <token>  或  X-API-Token: <token>
    # 留空 = 关闭鉴权（开发模式，启动时输出警告）；生产/联调环境务必设置
    api_token: str = ""

    # ---------- 阶段二：自动化执行 ----------
    playwright_headless: bool = True
    ops_platform_url: str = "http://localhost:3000/ops"

    # ---------- 派生属性 ----------
    @property
    def db_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
