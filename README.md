# VTCDocSense

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

## Kỷ luật token (bắt buộc)

- Mặc định **không gọi API LLM thật**: `LLM_PROVIDER=mock` phát lại fixture trong `tests/fixtures/llm_responses/`.
  Provider thật chỉ chạy khi `ALLOW_LIVE_LLM=true` hoặc CLI `--live`.
- Mọi lần gọi đi qua `app/llm/metered.py`: cache đĩa (khóa = sha256 nội dung trang + `PROMPT_VERSION` + model +
  schema), chặn `LLM_MAX_CALLS_PER_RUN` / `LLM_DAILY_BUDGET_USD`, ghi log token + chi phí, ghi bảng `llm_calls`.
- Đơn giá: `config/pricing.toml` (tự điền). `LLM_REQUIRE_PRICING=true` -> model chưa có giá bị từ chối gọi thật.
- Phân tầng: `MODEL_TEXT` (PDF có lớp chữ, phân loại) / `MODEL_VISION` (scan); escalate lên `MODEL_VISION` khi JSON
  sai schema hoặc trường trọng yếu low. JSON sai schema không retry cùng model.
- Ảnh gửi model: 150 DPI, ảnh xám, cắt lề trắng, bỏ trang trắng/bìa, cắt đôi trang A3.

```bash
python -m app.cli extract a.pdf --live --record   # chạy thật + ghi fixture (hỏi trước khi chạy!)
python -m app.cli cache stats|clear
python -m app.cli cost                            # chi phí thật hôm nay / ngân sách
```

Local CLI trên Windows: đặt `DATA_DIR=./data` (cache, sổ chi phí, upload); compose dùng `/data`.

## Job bất đồng bộ (M2)

```bash
curl -F "file=@lo.zip" localhost:8000/api/batches           # ZIP thư mục báo -> 202 + job_id từng PDF
curl -F "files=@a.pdf" -F folder_name="BÁO X" localhost:8000/api/documents
curl localhost:8000/api/batches/<id>                         # trạng thái theo document
curl localhost:8000/api/documents/<id>                       # kết quả JSON mới nhất
curl -OJ localhost:8000/api/documents/<id>/export.xlsx
curl localhost:8000/api/llm-calls                            # bảng llm_calls + tổng chi phí thật hôm nay
```

- Trạng thái: `uploaded → processing → needs_review | auto_approved | failed` (`approved/rejected`: M3).
  `auto_approved` chỉ khi số GP, ngày cấp, cơ quan báo chí, cơ quan chủ quản đều `high`.
- Xác minh không cần LLM (`app/pipeline/verify.py`): "Thời gian ký"/"SAO Y …; dd/mm/yyyy" trong lớp chữ;
  đối chiếu chéo "số X ngày Y" giữa các GP cùng thư mục báo (ghi `verified_by`).
- `/api/*` chưa có đăng nhập: chỉ mở khi `APP_ENV=local` hoặc đặt `INTERNAL_API_TOKEN`.

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

Pipeline (`app/pipeline`): `pdf` (pypdfium2, render 150 DPI ảnh xám kèm chữ ký số) → `preprocess` (cắt A3, bỏ trang trắng/bìa, xoay thẳng, sắp trang sổ gấp) → `classify` (rule trên lớp chữ; bản scan hỏi model) → `extract` (prompt theo mẫu, retry + fallback) → `validate` → `export`.

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
| `app/providers` | `ExtractionProvider`: `mock`, `gemini`, `openai`, `openai_compat`, `router`, `anthropic`, `vllm` |
| `app/llm` | Cache, sổ chi phí, ngân sách, đơn giá, record/replay fixture |
| `app/services` | Upload/ZIP, xử lý document trong worker, đối chiếu chéo |
| `app/models` | Schema output `v1` (Pydantic) + JSON Schema strict cho structured output |
| `app/pipeline` | pdf → preprocess → classify → extract → validate → verify → export |
| `eval/pdfs` | File mẫu + Excel đáp án (không commit) |
| `app/core` | config, logging JSON, readiness checks |
| `app/db`, `migrations/` | SQLAlchemy 2 async + Alembic (chạy `alembic upgrade head` khi api khởi động) |

## Khuyến nghị model (vLLM)

Mặc định `Qwen/Qwen3-VL-8B-Instruct`. Với GPU 24GB: dùng bản FP8/AWQ hoặc giảm `VLLM_MAX_MODEL_LEN`; chi tiết sẽ bổ sung ở `docs/DEPLOY.md` (M5).
