"""
统一配置中心（pydantic-settings）
=================================
- 环境变量前缀：BIZ_（如 BIZ_LLM_PROVIDER / BIZ_DB_HOST）
- 读取 backend/.env；全部配置集中于此，业务代码通过 settings 单例访问
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（.env 所在位置）
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BIZ_",
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- LLM ----------
    llm_provider: str = "deepseek"            # deepseek / openai / local / codebuddy / mock
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"  # 兼容旧模型名 deepseek-chat，可 env 覆盖
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
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
    chroma_dir: str = "./chroma_db"
    embedding_provider: str = "chroma_default"  # chroma_default / fastembed_bge_zh / openai

    # ---------- 数据模式 ----------
    # mock：工具返回模拟数据（骨架/无数据库环境演示）；real：真实查询 MySQL
    data_mode: str = "mock"

    # ---------- 阶段二：自动化执行 ----------
    playwright_headless: bool = True
    ops_platform_url: str = "http://localhost:3000/ops"

    # ---------- 派生属性 ----------
    @property
    def mock_mode(self) -> bool:
        """数据层是否走模拟：仅由 BIZ_DATA_MODE 决定（与 LLM 是否 Mock 解耦）。"""
        return self.data_mode == "mock"

    @property
    def mock_llm_active(self) -> bool:
        """LLM 是否降级为 Mock（未配置 API Key 时）。"""
        if self.llm_provider == "mock":
            return True
        if self.llm_provider == "deepseek" and not self.deepseek_api_key:
            return True
        if self.llm_provider == "openai" and not self.openai_api_key:
            return True
        return False

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
