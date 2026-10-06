from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["mock", "vllm", "anthropic", "openai_compat", "router"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    enable_docs: bool = True

    database_url: str = "postgresql+asyncpg://gpocr:gpocr@postgres:5432/gpocr"
    redis_url: str = "redis://redis:6379/0"

    data_dir: Path = Path("/data")
    retention_days: int = 30
    max_file_mb: int = 20
    max_pages: int = 30

    # Pipeline
    render_dpi: int = 200
    image_jpeg_quality: int = 85
    # Tỷ lệ trường "low" vượt ngưỡng -> gọi provider phụ (nếu FALLBACK_ENABLED)
    low_conf_fallback_ratio: float = 0.3

    # Provider
    llm_provider: ProviderName = "mock"
    fallback_enabled: bool = False
    fallback_provider: ProviderName | None = None
    provider_timeout_s: float = 120.0
    provider_max_retries: int = 2

    model_name: str = "Qwen/Qwen3-VL-8B-Instruct"
    vllm_base_url: str = "http://vllm:8000/v1"

    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"

    openai_compat_base_url: str | None = None
    openai_compat_api_key: SecretStr | None = None
    # 1 model, danh sách ưu tiên "a,b,c" (tự chuyển khi hết quota), hoặc "auto" (lấy từ GET /models)
    openai_compat_model: str | None = None
    # Regex lọc model khi dùng "auto", vd "gemini"
    openai_compat_model_filter: str | None = None
    # json_schema (strict) | json_object (schema trong prompt) - tự hạ cấp nếu endpoint trả 400
    openai_compat_response_format: Literal["json_schema", "json_object"] = "json_schema"

    # 9router (router OpenAI-compatible nội bộ), dùng làm provider chính hoặc dự phòng
    router_base_url: str | None = None
    router_api_key: SecretStr | None = None
    router_model: str = "auto"
    router_model_filter: str | None = None

    # Worker
    worker_concurrency: int = Field(default=2, ge=1)
    job_timeout_s: int = 900

    @field_validator(
        "fallback_provider",
        "anthropic_api_key",
        "openai_compat_base_url",
        "openai_compat_api_key",
        "openai_compat_model",
        "openai_compat_model_filter",
        "router_base_url",
        "router_api_key",
        "router_model_filter",
        mode="before",
    )
    @classmethod
    def _empty_as_none(cls, v: object) -> object:
        # Compose/Coolify truyền biến chưa đặt dưới dạng chuỗi rỗng
        return None if v == "" else v


@lru_cache
def get_settings() -> Settings:
    return Settings()
