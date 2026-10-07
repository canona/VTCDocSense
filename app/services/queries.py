"""Truy vấn dùng chung (không phụ thuộc service khác, tránh import vòng)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Extraction


async def latest_extraction(session: AsyncSession, doc_id: uuid.UUID) -> Extraction | None:
    stmt = (
        select(Extraction)
        .where(Extraction.document_id == doc_id)
        .order_by(Extraction.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
