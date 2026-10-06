"""Pipeline 1 file: pdf -> preprocess -> classify -> extract -> validate."""

import asyncio
import logging
import time
from io import BytesIO

from PIL import Image

from app.core.config import Settings
from app.models.schema import F, GiayPhep, GiayPhepCore, LoaiVanBan, Meta, PdfType, llm_json_schema
from app.pipeline import prompts
from app.pipeline.classify import Classification, ClassifyOut, classify_by_rules
from app.pipeline.extract import Usage, extract_with_fallback
from app.pipeline.pdf import RawPage, load_pdf, text_layer_ok
from app.pipeline.preprocess import LogicalPage, PreprocessResult, preprocess
from app.pipeline.validate import validate
from app.providers import ExtractionProvider, ExtractionRequest, PageInput

log = logging.getLogger(__name__)

CLASSIFY_MAX_SIDE = 1200  # px; phân loại không cần ảnh nét


def _jpeg(img: Image.Image, quality: int, max_side: int | None = None) -> bytes:
    if max_side and max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def pdf_type(raw: list[RawPage]) -> PdfType:
    good = [text_layer_ok(p.text) for p in raw]
    if all(good):
        return PdfType.TEXT
    if not any(good):
        return PdfType.SCAN
    return PdfType.MIXED


def build_inputs(pages: list[LogicalPage], quality: int) -> list[PageInput]:
    """Trang có lớp chữ tốt -> gửi text; trang 1 luôn kèm ảnh (số/ngày nằm trong chữ ký số/viết tay)."""
    out: list[PageInput] = []
    for p in pages:
        good = bool(p.text and text_layer_ok(p.text))
        need_image = p.image is not None and (not good or p.page_no == 1)
        out.append(
            PageInput(
                page_no=p.page_no,
                text=p.text if good else None,
                image=_jpeg(p.image, quality) if need_image and p.image else None,
                image_mime="image/jpeg",
            )
        )
    return out


async def classify(
    pages: list[LogicalPage],
    provider: ExtractionProvider,
    fallback: ExtractionProvider | None,
    settings: Settings,
    usage: Usage,
) -> Classification:
    rule = classify_by_rules(pages)
    if rule is not None:
        return rule
    inputs = [
        PageInput(page_no=p.page_no, image=_jpeg(p.image, 80, CLASSIFY_MAX_SIDE), image_mime="image/jpeg")
        for p in pages[:2]
        if p.image is not None
    ]
    req = ExtractionRequest(
        system_prompt=prompts.CLASSIFY_SYSTEM,
        user_prompt=prompts.CLASSIFY_PROMPT,
        pages=inputs,
        json_schema=llm_json_schema(ClassifyOut),
        schema_name="PhanLoai",
    )
    out, _ = await extract_with_fallback(
        provider, fallback, req, ClassifyOut, retries=settings.provider_max_retries, usage=usage
    )
    return Classification(out.loai_van_ban, out.ten_loai_giay_phep, "model", out.ly_do)


async def process_preprocessed(
    pre: PreprocessResult,
    *,
    file_name: str,
    physical_pages: int,
    kind: PdfType,
    provider: ExtractionProvider,
    settings: Settings,
    fallback: ExtractionProvider | None = None,
    started: float | None = None,
) -> GiayPhep:
    started = started if started is not None else time.perf_counter()
    usage = Usage()
    meta = Meta(file_name=file_name, pages=physical_pages, logical_pages=len(pre.pages), pdf_type=kind)

    if not pre.pages:
        core = GiayPhepCore(loai_van_ban=LoaiVanBan.KHAC)
        gp = validate(core, meta)
        gp.review_reasons.insert(0, "PDF không có trang nội dung (toàn trang trắng/bìa)")
        return gp

    cls = await classify(pre.pages, provider, fallback, settings, usage)
    meta.classified_by = cls.by
    log.info("phân loại", extra={"loai": cls.loai.value, "by": cls.by, "reason": cls.reason})

    if cls.loai == LoaiVanBan.KHAC:
        core = GiayPhepCore(loai_van_ban=LoaiVanBan.KHAC, ten_loai_giay_phep=cls.title)
        extra = [f"Lý do phân loại: {cls.reason}"] if cls.reason else []
    else:
        inputs = build_inputs(pre.pages, settings.image_jpeg_quality)
        req = ExtractionRequest(
            system_prompt=prompts.SYSTEM_PROMPT,
            user_prompt=prompts.extraction_user_prompt(
                cls.loai,
                has_images=any(i.image for i in inputs),
                has_text=any(i.text for i in inputs),
            ),
            pages=inputs,
            json_schema=llm_json_schema(GiayPhepCore),
        )
        core, res = await extract_with_fallback(
            provider,
            fallback,
            req,
            GiayPhepCore,
            retries=settings.provider_max_retries,
            usage=usage,
            low_conf_ratio=settings.low_conf_fallback_ratio,
        )
        meta.provider, meta.model = res.provider, res.model
        extra = []
        if core.loai_van_ban != cls.loai:
            log.info("model trả loai_van_ban khác phân loại", extra={"model": core.loai_van_ban.value})
            core.loai_van_ban = cls.loai
        if not core.ten_loai_giay_phep and cls.title:
            core.ten_loai_giay_phep = cls.title
        if cls.loai != LoaiVanBan.GP_MO_CHUYEN_TRANG and core.can_cu_gp_goc.value:
            # Trường này chỉ dành cho GP chuyên trang; model hay nhầm với phần "Căn cứ Luật ..."
            core.can_cu_gp_goc = F()

    if meta.provider is None and usage.calls:
        meta.provider, meta.model = usage.calls[-1].split("/", 1)
    meta.input_tokens, meta.output_tokens = usage.input_tokens, usage.output_tokens
    meta.duration_ms = int((time.perf_counter() - started) * 1000)
    gp = validate(core, meta)
    if extra:
        gp.review_reasons.extend(extra)
    return gp


async def process_pdf(
    data: bytes,
    file_name: str,
    provider: ExtractionProvider,
    settings: Settings,
    fallback: ExtractionProvider | None = None,
) -> GiayPhep:
    """Xử lý 1 file PDF. Ném `PdfError` nếu PDF hỏng/mã hóa/quá trang, `ProviderError` nếu model lỗi."""
    started = time.perf_counter()
    raw = await asyncio.to_thread(
        load_pdf, data, max_pages=settings.max_pages, max_mb=settings.max_file_mb, dpi=settings.render_dpi
    )
    pre = await asyncio.to_thread(preprocess, raw)
    log.info(
        "tiền xử lý",
        extra={
            "file": file_name,
            "pages": len(raw),
            "logical": [p.source for p in pre.pages],
            "dropped": [f"{p.source}:{p.kind}" for p in pre.dropped],
            "spreads": pre.spreads,
            "reordered": pre.reordered,
        },
    )
    return await process_preprocessed(
        pre,
        file_name=file_name,
        physical_pages=len(raw),
        kind=pdf_type(raw),
        provider=provider,
        settings=settings,
        fallback=fallback,
        started=started,
    )
