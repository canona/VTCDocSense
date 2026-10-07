from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["mock", "gemini", "openai", "anthropic", "openai_compat", "router", "vllm"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    enable_docs: bool = True

    database_url: str = "postgresql+asyncpg://docsense:docsense@postgres:5432/docsense"
    redis_url: str = "redis://redis:6379/0"

    data_dir: Path = Path("/data")
    retention_days: int = 30
    max_file_mb: int = 20
    max_pages: int = 30
    max_upload_mb: int = 100  # 1 request upload (ZIP); Cloudflare giới hạn 100MB

    # Bảo vệ API nội bộ (/api/*) cho tới khi có Cloudflare Access (M3):
    # có token -> bắt buộc "Authorization: Bearer <token>"; không có token -> chỉ mở khi APP_ENV=local
    internal_api_token: SecretStr | None = None

    # ----- Đăng nhập giao diện web -----
    # cf_access: xác minh JWT header Cf-Access-Jwt-Assertion (production);
    # dev: đăng nhập bằng email tự nhập (chỉ local, bị chặn khi APP_ENV=production)
    auth_mode: Literal["cf_access", "dev"] = "cf_access"
    cf_access_team_domain: str | None = None  # vd <team>.cloudflareaccess.com
    cf_access_aud: str | None = None  # Application Audience (AUD) tag
    bootstrap_admin_emails: str = ""  # "a@x.com,b@y.com": tự là admin khi đăng nhập
    api_key_pepper: SecretStr | None = None  # trộn vào hash API key (M4)

    # ----- API đối tác /v1 (M4) -----
    partner_docs: bool = True  # /v1/docs + /v1/openapi.json (chỉ route đối tác), độc lập ENABLE_DOCS
    api_rate_limit_per_minute: int = Field(default=60, ge=1)  # mặc định/key; tenant ghi đè được
    api_max_files_per_request: int = Field(default=20, ge=1)
    idempotency_ttl_hours: int = Field(default=24, ge=1)
    webhook_timeout_s: float = 10.0
    webhook_max_attempts: int = Field(default=8, ge=1)
    # true: cho webhook tới http:// và địa chỉ nội bộ (localhost/10.x/...). Chỉ bật khi dev/test (SSRF)
    webhook_allow_private: bool = False

    # Pipeline
    render_dpi: int = 150
    image_grayscale: bool = True
    image_jpeg_quality: int = 85
    # Tỷ lệ trường "low" vượt ngưỡng -> gọi provider phụ (nếu FALLBACK_ENABLED)
    low_conf_fallback_ratio: float = 0.3

    # ----- Kỷ luật token -----
    # Gọi API LLM thật (tốn tiền) chỉ khi bật cờ này (hoặc CLI --live); cache hit vẫn dùng được khi tắt
    allow_live_llm: bool = False
    llm_daily_budget_vnd: float = Field(default=50_000, ge=0)  # đồng/ngày
    llm_max_calls_per_run: int = Field(default=10, ge=0)  # 1 run = 1 lệnh CLI hoặc 1 document trong worker
    llm_cache_enabled: bool = True
    llm_cache_dir: Path | None = None  # mặc định <DATA_DIR>/llm_cache
    llm_ledger_dir: Path | None = None  # sổ chi phí theo ngày, mặc định <DATA_DIR>/llm_ledger
    llm_fixtures_dir: Path = Path("tests/fixtures/llm_responses")  # mock replay / --record
    pricing_file: Path = Path("config/pricing.toml")
    # true: model chưa có đơn giá -> từ chối gọi thật. false: vẫn gọi, chi phí ghi 0 (ngân sách ngày
    # không có tác dụng, chỉ còn LLM_MAX_CALLS_PER_RUN chặn)
    llm_require_pricing: bool = False
    # Phân tầng model (ghi đè model mặc định của provider): rẻ cho PDF có lớp chữ, mạnh cho scan
    model_text: str | None = None
    model_vision: str | None = None
    max_output_tokens: int = 8192
    # Mức "thinking" (none/minimal/low/medium/high); trống: gemini dùng "low", provider khác không gửi.
    # Endpoint trả 400 vì tham số này -> tự gửi lại không kèm
    llm_reasoning_effort: str | None = None
    classify_max_tokens: int = 300
    use_batch_api: bool = False  # dự phòng, chưa triển khai (xem README)

    # Provider
    llm_provider: ProviderName = "mock"
    fallback_enabled: bool = False
    fallback_provider: ProviderName | None = None
    provider_timeout_s: float = 120.0
    provider_max_retries: int = 2

    model_name: str = "Qwen/Qwen3-VL-8B-Instruct"
    vllm_base_url: str = "http://vllm:8000/v1"

    # LLM_PROVIDER=gemini|openai: OpenAI-compatible endpoint của nhà cung cấp
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_model: str | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str | None = None

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
        "internal_api_token",
        "cf_access_team_domain",
        "cf_access_aud",
        "api_key_pepper",
        "llm_cache_dir",
        "llm_ledger_dir",
        "model_text",
        "llm_reasoning_effort",
        "model_vision",
        "gemini_api_key",
        "gemini_model",
        "openai_api_key",
        "openai_model",
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

    @property
    def admin_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.bootstrap_admin_emails.split(",") if e.strip()}

    @property
    def cache_dir(self) -> Path:
        return self.llm_cache_dir or self.data_dir / "llm_cache"

    @property
    def ledger_dir(self) -> Path:
        return self.llm_ledger_dir or self.data_dir / "llm_ledger"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


@lru_cache
def get_settings() -> Settings:
    return Settings()
