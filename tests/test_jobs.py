"""Luồng M2: upload ZIP -> document + job -> worker (chạy inline, mock) -> trạng thái, llm_calls.

DB: SQLite (aiosqlite) tạo bằng metadata.create_all; không cần Postgres/Redis, không gọi API thật.
"""

import io
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.main import create_app
from app.api.routes.internal import get_session
from app.core.config import Settings, get_settings
from app.db import models
from app.db.session import Base
from app.providers.mock import MockProvider
from app.services.documents import process_document
from app.services.storage import read_zip
from tests.helpers import images_to_pdf, sample_extraction, text_page


class _NoUtf8Flag(zipfile.ZipInfo):
    """Mô phỏng ZIP tạo trên Windows: tên ghi bytes UTF-8 nhưng không bật cờ 0x800."""

    def _encodeFilenameFlags(self) -> tuple[bytes, int]:  # noqa: N802
        return self.filename.encode("utf-8"), self.flag_bits


def _zip(entries: dict[str, bytes], utf8: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(zipfile.ZipInfo(name) if utf8 else _NoUtf8Flag(name), data)
    return buf.getvalue()


def test_read_zip_folders_and_windows_names() -> None:
    pdf = images_to_pdf([text_page()])
    data = _zip(
        {"BÁO A/gp1.pdf": pdf, "BÁO A/ghi chú.txt": b"x", "__MACOSX/BÁO A/._gp1.pdf": pdf}, utf8=False
    )
    files = read_zip(data, max_file_bytes=10 * 1024 * 1024)
    assert [(f.folder_name, f.file_name) for f in files] == [("BÁO A", "gp1.pdf")]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[TestClient, Settings, MockProvider]]:
    settings = Settings(
        app_env="local",
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        render_dpi=72,
        llm_fixtures_dir=tmp_path / "fx",
    )
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    gp = sample_extraction()
    gp["so_gp"] = {"value": "635/GP-BTTTT", "confidence": "high"}
    provider = MockProvider(
        responses={"GiayPhep": gp, "PhanLoai": {"loai_van_ban": "GP_HOAT_DONG_LUAT_2016"}}
    )

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override

    async def enqueue_inline(doc_id: uuid.UUID) -> None:
        async with maker() as s:
            await process_document(s, settings, doc_id, provider)

    with TestClient(app) as client:

        async def init() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as s:
                s.add(models.Tenant(id=models.DEFAULT_TENANT_ID, slug="default", name="Nội bộ"))
                await s.commit()

        client.portal.call(init)  # type: ignore[union-attr]
        app.state.enqueue = enqueue_inline
        yield client, settings, provider
        client.portal.call(engine.dispose)  # type: ignore[union-attr]


def test_upload_zip_processes_and_logs_calls(env: tuple[TestClient, Settings, MockProvider]) -> None:
    client, settings, provider = env
    text_pdf = images_to_pdf([text_page()])  # ảnh không có lớp chữ -> phân loại bằng model (mock)
    data = _zip({"BÁO A/gp1.pdf": text_pdf, "BÁO A/gp2.pdf": text_pdf, "BÁO B/x.pdf": text_pdf})
    r = client.post(
        "/api/batches", files={"file": ("lo.zip", data, "application/zip")}, data={"auto_start": "true"}
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert len(body["documents"]) == 3 and all(d["job_id"].startswith("doc:") for d in body["documents"])

    b = client.get(f"/api/batches/{body['batch_id']}").json()
    assert sum(b["counts"].values()) == 3
    assert set(b["counts"]) <= {"needs_review", "auto_approved"}
    assert {d["folder_name"] for d in b["documents"]} == {"BÁO A", "BÁO B"}

    doc_id = body["documents"][0]["id"]
    d = client.get(f"/api/documents/{doc_id}").json()
    assert d["extraction"]["version"] == 1 and d["extraction"]["data"]["so_gp"]["value"] == "635/GP-BTTTT"

    x = client.get(f"/api/documents/{doc_id}/export.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"

    calls = client.get("/api/llm-calls").json()
    assert len(calls["calls"]) == 6  # 3 file x (phân loại + trích xuất)
    assert all(not c["live"] and c["cost_vnd"] == 0 for c in calls["calls"])
    assert calls["today_live"]["calls"] == 0
    assert len(provider.calls) == 6


def test_upload_rejects_non_pdf_and_failed_status(env: tuple[TestClient, Settings, MockProvider]) -> None:
    client, _, _ = env
    r = client.post("/api/documents", files={"files": ("a.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 415
    broken = b"%PDF-1.4 broken"
    r = client.post(
        "/api/documents", files={"files": ("b.pdf", broken, "application/pdf")}, data={"auto_start": "true"}
    )
    assert r.status_code == 202
    d = client.get(f"/api/documents/{r.json()['documents'][0]['id']}").json()
    assert d["status"] == "failed" and "PdfError" in d["error"]


@pytest.mark.parametrize(("so_conf", "expected"), [("high", "auto_approved"), ("low", "needs_review")])
def test_auto_approved_only_when_critical_high(
    env: tuple[TestClient, Settings, MockProvider], so_conf: str, expected: str
) -> None:
    client, _, provider = env
    gp = sample_extraction()
    gp["so_gp"] = {"value": "635/GP-BTTTT", "confidence": so_conf}
    gp["ngay_cap"] = {"value": "01/09/2021", "confidence": "high"}
    gp["co_quan_chu_quan"] = {"ten": {"value": "Tỉnh ủy An Giang", "confidence": "high"}}
    gp["co_quan_bao_chi"] = {"ten": {"value": "Báo An Giang", "confidence": "high"}}
    provider.responses["GiayPhep"] = gp
    r = client.post(
        "/api/documents",
        files={"files": ("c.pdf", images_to_pdf([text_page()]), "application/pdf")},
        data={"auto_start": "true"},
    )
    assert client.get(f"/api/documents/{r.json()['documents'][0]['id']}").json()["status"] == expected


def test_upload_waits_for_start(env: tuple[TestClient, Settings, MockProvider]) -> None:
    client, _, provider = env
    pdf = images_to_pdf([text_page()])
    r = client.post("/api/documents", files={"files": ("w.pdf", pdf, "application/pdf")}).json()
    assert r["started"] is False and r["estimate"]["documents"] == 1 and r["estimate"]["scan_pages"] == 1
    assert r["documents"][0]["status"] == "uploaded" and provider.calls == []
    s = client.post(f"/api/batches/{r['batch_id']}/start").json()
    assert s["started"] == 1
    status = client.get(f"/api/documents/{r['documents'][0]['id']}").json()["status"]
    assert status in ("needs_review", "auto_approved") and provider.calls


def test_internal_api_closed_outside_local(env: tuple[TestClient, Settings, MockProvider]) -> None:
    client, settings, _ = env
    client.app.dependency_overrides[get_settings] = lambda: settings.model_copy(  # type: ignore[attr-defined]
        update={"app_env": "production"}
    )
    assert client.get("/api/documents").status_code == 403
