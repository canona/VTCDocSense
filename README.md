# GP-OCR

OCR & bóc tách trường Giấy phép báo chí → JSON + Excel. Self-host bằng Coolify, public qua Cloudflare Tunnel.

## Chạy local

```bash
cp .env.example .env        # đặt POSTGRES_PASSWORD, REDIS_PASSWORD, APP_ENV=local
docker compose up -d --build
curl localhost:8000/healthz
curl localhost:8000/readyz  # db, redis, worker, provider
docker compose exec api python -m app.cli check
```

- `docker-compose.override.yml` chỉ dùng local (publish `127.0.0.1:8000`, bật `/docs`). Coolify chỉ đọc `docker-compose.yml`.
- Có GPU: `docker compose --profile gpu up -d` và đặt `LLM_PROVIDER=vllm`.
- Tunnel trong compose (thay cho tunnel của Coolify): `--profile tunnel` + `TUNNEL_TOKEN`.

## Dev

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Linux: .venv/bin/pip
pytest && ruff check . && ruff format --check . && mypy app
```

## Cấu trúc

| Thư mục | Nội dung |
|---|---|
| `app/api` | FastAPI, routes |
| `app/worker` | arq worker (`arq app.worker.settings.WorkerSettings`) |
| `app/providers` | `ExtractionProvider`: `mock`, `vllm`, `openai_compat`, `anthropic` (M1) |
| `app/pipeline` | pdf → preprocess → classify → extract → validate → crosscheck → export (M1+) |
| `app/core` | config, logging JSON, readiness checks |
| `app/db`, `migrations/` | SQLAlchemy 2 async + Alembic (chạy `alembic upgrade head` khi api khởi động) |

## Khuyến nghị model (vLLM)

Mặc định `Qwen/Qwen3-VL-8B-Instruct`. Với GPU 24GB: dùng bản FP8/AWQ hoặc giảm `VLLM_MAX_MODEL_LEN`; chi tiết sẽ bổ sung ở `docs/DEPLOY.md` (M5).
