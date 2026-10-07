"""Ảnh trang cho màn hình rà soát: trang logic sau tiền xử lý (cắt A3, bỏ bìa/trắng/trùng),
khớp đánh số `source_page` của kết quả. Render 1 lần, cache PNG trên volume."""

import asyncio
import json
from pathlib import Path

from app.core.config import Settings
from app.pipeline.pdf import load_pdf
from app.pipeline.preprocess import preprocess

VIEW_DPI = 110


def _cache_dir(settings: Settings, doc_id: str) -> Path:
    return settings.data_dir / "page_cache" / doc_id


def _render(settings: Settings, doc_id: str, pdf_path: Path) -> list[str]:
    out = _cache_dir(settings, doc_id)
    if not pdf_path.exists():  # đã xóa theo RETENTION_DAYS
        return []
    raw = load_pdf(
        pdf_path.read_bytes(),
        max_pages=settings.max_pages,
        max_mb=settings.max_file_mb,
        dpi=VIEW_DPI,
        grayscale=False,
        render_all=True,
    )
    pre = preprocess(raw, do_deskew=False)
    pages = pre.pages or [p for p in pre.dropped]  # không có trang nội dung -> vẫn cho xem
    out.mkdir(parents=True, exist_ok=True)
    labels = []
    for i, p in enumerate(pages, start=1):
        if p.image is not None:
            p.image.save(out / f"{i}.png", optimize=True)
        labels.append(p.source)
    (out / "pages.json").write_text(json.dumps(labels), encoding="utf-8")
    return labels


async def page_labels(settings: Settings, doc_id: str, pdf_path: Path) -> list[str]:
    """Nhãn trang vật lý của từng trang logic (vd ["2L", "2R"]); render nếu chưa có cache."""
    meta = _cache_dir(settings, doc_id) / "pages.json"
    if meta.exists():
        return json.loads(meta.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    return await asyncio.to_thread(_render, settings, doc_id, pdf_path)


def page_file(settings: Settings, doc_id: str, n: int) -> Path:
    return _cache_dir(settings, doc_id) / f"{n}.png"
