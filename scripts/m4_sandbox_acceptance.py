"""Nghiệm thu M4: mô phỏng một đối tác tích hợp bằng key SANDBOX, đi hết luồng
upload -> nhận webhook (xác minh chữ ký HMAC) -> tải kết quả JSON + Excel -> kiểm tra usage,
và chứng minh 0 lần gọi LLM.

Chế độ 1 - tự chạy trọn gói trên máy (mặc định, không cần Docker/Postgres/Redis):
    python scripts/m4_sandbox_acceptance.py
  Dựng API thật (uvicorn) trên 127.0.0.1 với SQLite tạm, worker chạy trong tiến trình, provider LLM bị
  thay bằng provider "cấm gọi" (gọi là lỗi ngay). Đối tác gọi API qua HTTP như bình thường.

Chế độ 2 - chạy với hệ thống đã triển khai (staging/production):
    python scripts/m4_sandbox_acceptance.py --base-url https://apidocsense.vtcdigital.top \\
        --api-key ds_test_xxx --webhook-secret whsec_xxx \\
        --webhook-listen 0.0.0.0:8765 --webhook-public-url https://<url công khai tới cổng 8765>/hook
  Không có URL công khai cho webhook: thêm --no-webhook (chỉ poll trạng thái).
  Script từ chối chạy với key ds_live_ (tránh tốn tiền).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import io
import json
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
OK, FAIL = "  [OK] ", "  [LỖI]"


# ---------------- Phía đối tác: nhận + xác minh webhook (như ví dụ trong docs/PARTNER_API.md)


def verify_signature(secret: str, body: bytes, header: str, tolerance_s: int = 300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


class Receiver:
    def __init__(self, host: str, port: int) -> None:
        self.events: queue.Queue[tuple[dict[str, str], bytes]] = queue.Queue()
        events = self.events

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                events.put(({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, n: int, timeout: float) -> list[tuple[dict[str, str], bytes]]:
        got, end = [], time.time() + timeout
        while len(got) < n and time.time() < end:
            try:
                got.append(self.events.get(timeout=max(0.1, end - time.time())))
            except queue.Empty:
                break
        return got


# ---------------- Tài liệu mẫu ----------------


def sample_pdf(pages: int = 2) -> bytes:
    from PIL import Image, ImageDraw

    imgs = []
    for p in range(pages):
        img = Image.new("RGB", (600, 850), "white")
        d = ImageDraw.Draw(img)
        for i in range(28):
            y = 100 + i * 23
            d.rectangle((70, y, 70 + 380 + (i * 37 + p * 11) % 90, y + 7), fill="black")
        imgs.append(img)
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:], resolution=72.0)
    return buf.getvalue()


# ---------------- Dựng hệ thống tại chỗ (chế độ 1) ----------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LocalStack:
    """API thật (uvicorn) + SQLite tạm + worker trong tiến trình; provider LLM bị cấm."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="docsense_m4_"))
        os.environ.update(
            APP_ENV="local",
            LOG_LEVEL="WARNING",
            DATA_DIR=str(self.tmp),
            DATABASE_URL=f"sqlite+aiosqlite:///{(self.tmp / 'db.sqlite').as_posix()}",
            REDIS_URL="redis://127.0.0.1:1/0",
            LLM_PROVIDER="mock",
            ALLOW_LIVE_LLM="false",
            WEBHOOK_ALLOW_PRIVATE="true",  # receiver chạy trên 127.0.0.1
            AUTH_MODE="dev",
        )
        sys.path.insert(0, str(ROOT))
        from app.core.config import Settings, get_settings

        Settings.model_config["env_file"] = None  # không đọc .env (có thể chứa key thật)
        get_settings.cache_clear()
        self.settings = get_settings()
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.forbidden_calls = 0
        self.tasks: set[asyncio.Task[None]] = set()

    def _make_provider(self) -> Any:
        from app.providers.base import ExtractionProvider, ExtractionRequest, ExtractionResult, ProviderError

        stack = self

        class ForbiddenProvider(ExtractionProvider):
            name, model = "forbidden", "forbidden"

            async def extract(self, req: ExtractionRequest) -> ExtractionResult:
                stack.forbidden_calls += 1
                raise ProviderError("Sandbox KHÔNG được gọi LLM")

            async def health(self) -> bool:
                return True

        return ForbiddenProvider()

    async def _init_db(self) -> tuple[str, str]:
        from app.db import models
        from app.db.session import Base, get_engine, get_sessionmaker
        from app.services.api_keys import new_api_key
        from app.services.webhooks import new_secret

        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with get_sessionmaker()() as s:
            tenant = models.Tenant(
                slug="doitac-demo",
                name="Đối tác demo",
                require_human_review=True,
                webhook_secret=new_secret(),
            )
            s.add(tenant)
            await s.flush()
            key, raw = new_api_key(self.settings, tenant.id, "M4 nghiệm thu", sandbox=True)
            s.add(key)
            await s.commit()
            return raw, str(tenant.webhook_secret)

    def start(self) -> tuple[str, str]:
        import uvicorn

        from app.api.main import create_app
        from app.db.session import get_sessionmaker
        from app.services import webhooks
        from app.services.documents import process_document
        from app.services.ratelimit import MemoryRateLimiter

        self.app = create_app()
        provider = self._make_provider()
        settings = self.settings
        http = httpx.AsyncClient(follow_redirects=False)

        async def work(doc_id: uuid.UUID) -> None:  # = job arq process_document + gửi webhook
            await asyncio.sleep(0.5)
            async with get_sessionmaker()() as s:
                await process_document(s, settings, doc_id, provider)
                await webhooks.deliver_due(s, settings, http)

        async def enqueue(doc_id: uuid.UUID, read_cache: bool = True, *, rerun: bool = False) -> None:
            t = asyncio.create_task(work(doc_id))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)

        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"
        )
        self.server = uvicorn.Server(config)
        threading.Thread(target=self.server.run, daemon=True).start()
        while not self.server.started:
            time.sleep(0.05)
        self.app.state.enqueue = enqueue
        self.app.state.rate_limiter = MemoryRateLimiter()
        fut = asyncio.run_coroutine_threadsafe(self._init_db(), self._loop())
        return fut.result(timeout=30)

    def _loop(self) -> asyncio.AbstractEventLoop:
        return _LOOP[0]  # loop của uvicorn (thread riêng): DB/engine gắn với loop này

    def llm_calls_in_db(self) -> int:
        from sqlalchemy import func, select

        from app.db.models import LlmCall
        from app.db.session import get_sessionmaker

        async def count() -> int:
            async with get_sessionmaker()() as s:
                return int(await s.scalar(select(func.count()).select_from(LlmCall)) or 0)

        return asyncio.run_coroutine_threadsafe(count(), self._loop()).result(timeout=30)

    def ledger_entries(self) -> int:
        d = self.settings.ledger_dir
        return (
            sum(1 for f in d.glob("*.jsonl") for line in f.read_text().splitlines() if line.strip())
            if d.exists()
            else 0
        )

    def stop(self) -> None:
        self.server.should_exit = True


_LOOP: list[asyncio.AbstractEventLoop] = []


# ---------------- Kịch bản đối tác ----------------


class Check:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, cond: bool, msg: str) -> bool:
        print(f"{OK if cond else FAIL} {msg}")
        self.failed += not cond
        return cond


def run_partner_flow(
    base_url: str, api_key: str, secret: str | None, receiver: Receiver | None, hook_url: str | None
) -> Check:
    check = Check()
    h = {"Authorization": f"Bearer {api_key}"}
    c = httpx.Client(base_url=base_url, headers=h, timeout=60)

    print("1) Kiểm tra key & schema")
    r = c.get("/v1/schema")
    check(r.status_code == 200 and "json_schema" in r.json(), f"GET /v1/schema -> {r.status_code}")
    r = httpx.get(f"{base_url}/v1/documents", timeout=30)
    check(
        r.status_code == 401 and r.json()["error"]["code"] == "missing_api_key",
        "Thiếu key -> 401 missing_api_key",
    )
    usage0 = c.get("/v1/usage").json()
    check(usage0.get("mode") == "sandbox", f"Key ở chế độ sandbox (mode={usage0.get('mode')})")

    print("2) Upload PDF (202 + Idempotency-Key)")
    pdf = sample_pdf(2)
    ext_id = f"HS-{uuid.uuid4().hex[:8]}"
    idem = str(uuid.uuid4())
    data: dict[str, Any] = {"external_id": ext_id}
    if hook_url:
        data["webhook_url"] = hook_url
    files = {"files": ("GiayPhep_demo.pdf", pdf, "application/pdf")}
    r = c.post("/v1/documents", files=files, data=data, headers={"Idempotency-Key": idem})
    if not check(r.status_code == 202, f"POST /v1/documents -> {r.status_code}"):
        print(r.text)
        return check
    doc = r.json()["documents"][0]
    doc_id = doc["id"]
    print(f"       document {doc_id} status={doc['status']}")
    r2 = c.post("/v1/documents", files=files, data=data, headers={"Idempotency-Key": idem})
    check(
        r2.status_code == 202 and r2.headers.get("Idempotent-Replayed") == "true" and r2.json() == r.json(),
        "Gửi lại cùng Idempotency-Key -> cùng response, không tạo document mới",
    )

    print("3) Chờ webhook")
    if receiver and hook_url:
        got = receiver.wait(1, timeout=60)
        if check(len(got) == 1, f"Nhận {len(got)} webhook"):
            headers, body = got[0]
            event = json.loads(body)
            check(event["type"] == "document.completed", f"Sự kiện {event['type']}")
            check(event["data"]["document"]["id"] == doc_id, "Webhook đúng document")
            check(event["data"]["document"]["external_id"] == ext_id, "Webhook mang external_id của đối tác")
            sig = headers.get("x-docsense-signature", "")
            if secret:
                check(verify_signature(secret, body, sig), "Chữ ký X-DocSense-Signature hợp lệ (HMAC-SHA256)")
                check(not verify_signature(secret, body + b"x", sig), "Body bị sửa -> chữ ký sai")
            else:
                print("       (bỏ qua xác minh chữ ký: không có --webhook-secret)")
    else:
        print("       (không dùng webhook: poll trạng thái)")
    status, end = None, time.time() + 120
    while time.time() < end:
        status = c.get(f"/v1/documents/{doc_id}").json()["status"]
        if status in ("completed", "failed", "rejected"):
            break
        time.sleep(1)
    check(status == "completed", f"Trạng thái cuối: {status}")

    print("4) Tải kết quả")
    d = c.get(f"/v1/documents/{doc_id}").json()
    res = d.get("result") or {}
    check(bool(res.get("so_gp", {}).get("value")), f"JSON: so_gp={res.get('so_gp', {}).get('value')}")
    check(d.get("sandbox") is True, "Kết quả đánh dấu sandbox=true")
    x = c.get(f"/v1/documents/{doc_id}/export.xlsx")
    check(
        x.status_code == 200 and x.content[:2] == b"PK",
        f"export.xlsx -> {x.status_code}, {len(x.content)} byte",
    )
    z = c.get(f"/v1/batches/{d['batch_id']}/export.zip")
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist() if z.status_code == 200 else []
    check("TongHop.xlsx" in names, f"Batch export.zip -> {names}")
    hooks = c.get(f"/v1/documents/{doc_id}/webhooks").json()
    if hook_url:
        check(any(x["status"] == "succeeded" for x in hooks), "Lịch sử webhook: đã gửi thành công")

    print("5) Usage")
    u = c.get("/v1/usage").json()
    print(f"       {u}")
    check(
        u["llm_calls"] == 0 and u["cost_vnd"] == 0,
        f"Usage sandbox: llm_calls={u['llm_calls']}, cost={u['cost_vnd']}",
    )
    return check


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # console Windows mặc định cp1252
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url")
    ap.add_argument("--api-key", help="Key ds_test_ (bắt buộc với --base-url)")
    ap.add_argument("--webhook-secret")
    ap.add_argument("--webhook-listen", default="127.0.0.1:0", help="host:port receiver webhook")
    ap.add_argument("--webhook-public-url", help="URL công khai trỏ tới receiver (chế độ 2)")
    ap.add_argument("--no-webhook", action="store_true")
    args = ap.parse_args()

    host, port = args.webhook_listen.rsplit(":", 1)
    receiver = None if args.no_webhook else Receiver(host, int(port))

    if args.base_url:
        if not args.api_key or not args.api_key.startswith("ds_test_"):
            print("Chỉ chạy với key sandbox ds_test_ (không tốn tiền).", file=sys.stderr)
            return 2
        hook_url = None if args.no_webhook else args.webhook_public_url
        if receiver and not hook_url:
            print("Cần --webhook-public-url (hoặc --no-webhook).", file=sys.stderr)
            return 2
        print(f"== Nghiệm thu M4 với {args.base_url} ==")
        check = run_partner_flow(args.base_url, args.api_key, args.webhook_secret, receiver, hook_url)
        print(f"\n{'ĐẠT' if check.failed == 0 else f'KHÔNG ĐẠT ({check.failed} lỗi)'}")
        return 0 if check.failed == 0 else 1

    print("== Nghiệm thu M4 (hệ thống tại chỗ: uvicorn + SQLite tạm, LLM bị cấm) ==")
    stack = LocalStack()
    _patch_loop_capture()
    raw_key, secret = stack.start()
    print(f"API: {stack.base_url}  | key: {raw_key[:14]}…  | dữ liệu tạm: {stack.tmp}")
    hook_url = f"http://127.0.0.1:{receiver.port}/hook" if receiver else None
    try:
        check = run_partner_flow(stack.base_url, raw_key, secret, receiver, hook_url)
        print("6) Chứng minh 0 lần gọi LLM")
        check(stack.forbidden_calls == 0, f"Provider LLM được gọi {stack.forbidden_calls} lần")
        n_db = stack.llm_calls_in_db()
        check(n_db == 0, f"Bảng llm_calls: {n_db} bản ghi")
        n_ledger = stack.ledger_entries()
        check(n_ledger == 0, f"Sổ chi phí LLM (ledger): {n_ledger} lượt gọi thật")
    finally:
        stack.stop()
    print(f"\n{'ĐẠT' if check.failed == 0 else f'KHÔNG ĐẠT ({check.failed} lỗi)'}")
    return 0 if check.failed == 0 else 1


def _patch_loop_capture() -> None:
    """Lấy event loop của uvicorn (để chạy truy vấn DB trên đúng loop)."""
    import uvicorn

    orig = uvicorn.Server.serve

    async def serve(self: Any, *a: Any, **kw: Any) -> None:
        _LOOP.append(asyncio.get_running_loop())
        await orig(self, *a, **kw)

    uvicorn.Server.serve = serve  # type: ignore[method-assign]


if __name__ == "__main__":
    sys.exit(main())
