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

## Bóc tách 1 file (M1)

```bash
python -m app.cli extract "eval/pdfs/BÁO AN GIANG/GP In Báo An Giang 2011_1.pdf" --out out/
# -> out/<tên>.json + out/<tên>.xlsx (sheet ThongTin / AnPham / LanhDao)
python -m app.cli extract a.pdf b.pdf --provider mock      # chạy thử không gọi model
```

Chưa có GPU → dùng API đám mây qua `openai_compat` (đặt trong `.env`, xem `.env.example`):

| Nhà cung cấp | `OPENAI_COMPAT_BASE_URL` |
|---|---|
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` |
| OpenAI | `https://api.openai.com/v1` |

`OPENAI_COMPAT_MODEL` phải là model đọc được ảnh. Nếu endpoint không nhận `json_schema` strict, provider tự chuyển sang `json_object`.

Pipeline (`app/pipeline`): `pdf` (pypdfium2, render 200 DPI kèm chữ ký số) → `preprocess` (cắt A3, bỏ trang trắng/bìa, xoay thẳng, sắp trang sổ gấp) → `classify` (rule trên lớp chữ; bản scan hỏi model) → `extract` (prompt theo mẫu, retry + fallback) → `validate` → `export`.

- PDF có lớp chữ: gửi text + ảnh trang 1, vì số GP/ngày cấp thường nằm trong hình chữ ký số của văn thư, không có trong lớp chữ.
- Danh sách người ký hợp lệ: `app/pipeline/signers.txt` (bổ sung dần).
- GP hoạt động báo chí **điện tử** cấp theo Luật 1989 được xếp vào `GP_HOAT_DONG_LUAT_1989` (phân loại theo luật làm căn cứ).

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
| `app/providers` | `ExtractionProvider`: `mock`, `vllm`, `openai_compat`, `anthropic` |
| `app/models` | Schema output `v1` (Pydantic) + JSON Schema strict cho structured output |
| `app/pipeline` | pdf → preprocess → classify → extract → validate → export (crosscheck: M2) |
| `eval/pdfs` | File mẫu + Excel đáp án (không commit) |
| `app/core` | config, logging JSON, readiness checks |
| `app/db`, `migrations/` | SQLAlchemy 2 async + Alembic (chạy `alembic upgrade head` khi api khởi động) |

## Khuyến nghị model (vLLM)

Mặc định `Qwen/Qwen3-VL-8B-Instruct`. Với GPU 24GB: dùng bản FP8/AWQ hoặc giảm `VLLM_MAX_MODEL_LEN`; chi tiết sẽ bổ sung ở `docs/DEPLOY.md` (M5).
