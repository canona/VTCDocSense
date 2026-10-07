"""Sandbox (key ds_test_): trả kết quả mẫu cố định từ fixture, KHÔNG BAO GIỜ gọi LLM.

Mô phỏng đúng vòng đời bản thật để đối tác tích hợp miễn phí:
- tenant require_human_review=true  -> coi như người duyệt đã duyệt ngay (completed, needs_review=false)
- tenant require_human_review=false -> completed kèm needs_review=true (fixture có trường confidence medium)
- `sandbox_scenario=failed|rejected` khi upload -> mô phỏng document lỗi / bị từ chối.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DocStatus, Document, Extraction, Tenant
from app.models.schema import SCHEMA_VERSION, GiayPhep
from app.services.review import sync_summary

FIXTURE = Path(__file__).resolve().parent.parent / "sandbox" / "giay_phep_mau.json"
SANDBOX_ACTOR = "sandbox"


@lru_cache
def _fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def sample_result(doc: Document) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_fixture())
    data["meta"].update(file_name=doc.file_name, pages=doc.pages or 1, pdf_type=doc.pdf_type or "SCAN")
    return GiayPhep.model_validate(data).model_dump(mode="json")


async def process_sandbox(session: AsyncSession, doc: Document) -> DocStatus:
    """Gán kết quả mẫu cho document sandbox (chưa commit)."""
    scenario = doc.sandbox_scenario or "completed"
    if scenario == "failed":
        doc.status, doc.error = DocStatus.failed, "SandboxError: mô phỏng xử lý thất bại (sandbox_scenario)"
        return DocStatus.failed
    tenant = await session.get(Tenant, doc.tenant_id)
    assert tenant is not None
    data = sample_result(doc)
    prev = await session.scalar(select(func.max(Extraction.version)).where(Extraction.document_id == doc.id))
    session.add(
        Extraction(
            document_id=doc.id,
            version=(prev or 0) + 1,
            data=data,
            schema_version=SCHEMA_VERSION,
            provider=SANDBOX_ACTOR,
            model="fixture",
            needs_review=bool(data["needs_review"]),
            created_by=SANDBOX_ACTOR,
        )
    )
    sync_summary(doc, data)
    doc.pdf_type = data["meta"]["pdf_type"]
    if scenario == "rejected":
        doc.status, doc.reviewed_by = DocStatus.rejected, SANDBOX_ACTOR
    elif tenant.require_human_review:
        doc.status, doc.reviewed_by = DocStatus.approved, SANDBOX_ACTOR
    else:
        doc.status = DocStatus.needs_review
    return DocStatus(doc.status)
