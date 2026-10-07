"""Pipeline 1 file: pdf -> preprocess -> classify -> extract -> validate -> verify (lớp chữ)."""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from io import BytesIO

from PIL import Image

from app.core.config import Settings
from app.models.schema import (
    Confidence,
    F,
    GiayPhep,
    GiayPhepCore,
    LoaiVanBan,
    Meta,
    PdfType,
    llm_json_schema,
)
from app.pipeline import prompts
from app.pipeline.classify import Classification, ClassifyOut, classify_by_rules
from app.pipeline.extract import SchemaError, Usage, extract_with_fallback
from app.pipeline.pdf import RawPage, load_pdf, text_layer_ok
from app.pipeline.preprocess import LogicalPage, PreprocessResult, preprocess, trim_margins
from app.pipeline.validate import validate
from app.pipeline.verify import GpRef, apply_text_evidence, refs_from_text, text_evidence
from app.providers import ExtractionProvider, ExtractionRequest, ExtractionResult, PageInput, ProviderError

log = logging.getLogger(__name__)

CLASSIFY_MAX_SIDE = 1200  # px; phân loại không cần ảnh nét

# Trường trọng yếu: quyết định escalate model và auto_approved
CRITICAL_FIELDS = ("so_gp", "ngay_cap", "co_quan_bao_chi.ten", "co_quan_chu_quan.ten")


def _jpeg(img: Image.Image, quality: int, max_side: int | None = None, gray: bool = True) -> bytes:
    if max_side and max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.convert("L" if gray else "RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def critical_fields(gp: GiayPhepCore) -> list[F]:  # type: ignore[type-arg]
    out = []
    for path in CRITICAL_FIELDS:
        obj: object = gp
        for part in path.split("."):
            obj = getattr(obj, part)
        assert isinstance(obj, F)
        out.append(obj)
    return out


def critical_low(gp: GiayPhepCore) -> int:
    """Số trường trọng yếu thiếu hoặc confidence=low."""
    return sum(1 for f in critical_fields(gp) if f.value is None or f.confidence == Confidence.low)


def all_critical_high(gp: GiayPhepCore) -> bool:
    return all(f.value is not None and f.confidence == Confidence.high for f in critical_fields(gp))


def pdf_type(raw: list[RawPage]) -> PdfType:
    good = [text_layer_ok(p.text) for p in raw]
    if all(good):
        return PdfType.TEXT
    if not any(good):
        return PdfType.SCAN
    return PdfType.MIXED


def build_inputs(
    pages: list[LogicalPage], quality: int, *, gray: bool = True, source_id: str | None = None
) -> list[PageInput]:
    """Trang có lớp chữ tốt -> gửi text; trang 1 luôn kèm ảnh (số/ngày nằm trong chữ ký số/viết tay).

    Ảnh: cắt lề trắng, ảnh xám. `source_id` (sha256 PDF + tham số render) làm khóa cache ổn định.
    """
    out: list[PageInput] = []
    for p in pages:
        good = bool(p.text and text_layer_ok(p.text))
        need_image = p.image is not None and (not good or p.page_no == 1)
        image = _jpeg(trim_margins(p.image), quality, gray=gray) if need_image and p.image else None
        out.append(
            PageInput(
                page_no=p.page_no,
                text=p.text if good else None,
                image=image,
                image_mime="image/jpeg",
                image_id=f"{source_id}:{p.source}" if image and source_id else None,
            )
        )
    return out


async def classify(
    pages: list[LogicalPage],
    provider: ExtractionProvider,
    fallback: ExtractionProvider | None,
    settings: Settings,
    usage: Usage,
    source_id: str | None = None,
) -> Classification:
    rule = classify_by_rules(pages)
    if rule is not None:
        return rule
    inputs = [
        PageInput(
            page_no=p.page_no,
            image=_jpeg(trim_margins(p.image), 80, CLASSIFY_MAX_SIDE, gray=settings.image_grayscale),
            image_mime="image/jpeg",
            image_id=f"{source_id}:{p.source}:cls" if source_id else None,
        )
        for p in pages[:2]
        if p.image is not None
    ]
    req = ExtractionRequest(
        system_prompt=prompts.CLASSIFY_SYSTEM,
        user_prompt=prompts.CLASSIFY_PROMPT,
        pages=inputs,
        json_schema=llm_json_schema(ClassifyOut),
        schema_name="PhanLoai",
        model=settings.model_text,  # phân loại là việc dễ -> model rẻ
        max_tokens=settings.classify_max_tokens,
    )
    out, _ = await extract_with_fallback(
        provider, fallback, req, ClassifyOut, retries=settings.provider_max_retries, usage=usage
    )
    return Classification(out.loai_van_ban, out.ten_loai_giay_phep, "model", out.ly_do)


async def extract_tiered(
    provider: ExtractionProvider,
    fallback: ExtractionProvider | None,
    req: ExtractionRequest,
    settings: Settings,
    usage: Usage,
) -> tuple[GiayPhepCore, ExtractionResult]:
    """Gọi với model theo tầng; escalate lên MODEL_VISION khi JSON sai schema hoặc trường trọng yếu low."""

    async def call(r: ExtractionRequest) -> tuple[GiayPhepCore, ExtractionResult]:
        return await extract_with_fallback(
            provider,
            fallback,
            r,
            GiayPhepCore,
            retries=settings.provider_max_retries,
            usage=usage,
            low_conf_ratio=settings.low_conf_fallback_ratio,
        )

    strong = settings.model_vision
    can_escalate = bool(strong) and req.model != strong
    escalated = req.model_copy(update={"model": strong})
    try:
        core, res = await call(req)
    except SchemaError:
        if not can_escalate:
            raise
        log.warning("JSON sai schema, escalate model", extra={"from": req.model, "to": strong})
        return await call(escalated)
    if can_escalate and critical_low(core):
        log.warning("trường trọng yếu low, escalate model", extra={"from": req.model, "to": strong})
        try:
            core2, res2 = await call(escalated)
        except ProviderError:
            return core, res
        if critical_low(core2) <= critical_low(core):
            return core2, res2
    return core, res


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
    source_id: str | None = None,
) -> GiayPhep:
    started = started if started is not None else time.perf_counter()
    usage = Usage()
    meta = Meta(file_name=file_name, pages=physical_pages, logical_pages=len(pre.pages), pdf_type=kind)

    if not pre.pages:
        core = GiayPhepCore(loai_van_ban=LoaiVanBan.KHAC)
        gp = validate(core, meta)
        gp.review_reasons.insert(0, "PDF không có trang nội dung (toàn trang trắng/bìa)")
        return gp

    cls = await classify(pre.pages, provider, fallback, settings, usage, source_id)
    meta.classified_by = cls.by
    log.info("phân loại", extra={"loai": cls.loai.value, "by": cls.by, "reason": cls.reason})

    if cls.loai == LoaiVanBan.KHAC:
        core = GiayPhepCore(loai_van_ban=LoaiVanBan.KHAC, ten_loai_giay_phep=cls.title)
        extra = [f"Lý do phân loại: {cls.reason}"] if cls.reason else []
    else:
        inputs = build_inputs(
            pre.pages, settings.image_jpeg_quality, gray=settings.image_grayscale, source_id=source_id
        )
        req = ExtractionRequest(
            system_prompt=prompts.SYSTEM_PROMPT,
            user_prompt=prompts.extraction_user_prompt(
                cls.loai,
                has_images=any(i.image for i in inputs),
                has_text=any(i.text for i in inputs),
            ),
            pages=inputs,
            json_schema=llm_json_schema(GiayPhepCore),
            # PDF có lớp chữ -> model rẻ; scan/hỗn hợp -> model mạnh
            model=settings.model_text if kind == PdfType.TEXT else settings.model_vision,
            max_tokens=settings.max_output_tokens,
        )
        core, res = await extract_tiered(provider, fallback, req, settings, usage)
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


@dataclass
class PdfOutcome:
    gp: GiayPhep
    sha256: str
    text_refs: list[GpRef] = field(default_factory=list)  # GP cũ được trích dẫn trong lớp chữ


async def process_pdf(
    data: bytes,
    file_name: str,
    provider: ExtractionProvider,
    settings: Settings,
    fallback: ExtractionProvider | None = None,
) -> GiayPhep:
    """Xử lý 1 file PDF. Ném `PdfError` nếu PDF hỏng/mã hóa/quá trang, `ProviderError` nếu model lỗi."""
    return (await process_pdf_full(data, file_name, provider, settings, fallback)).gp


async def process_pdf_full(
    data: bytes,
    file_name: str,
    provider: ExtractionProvider,
    settings: Settings,
    fallback: ExtractionProvider | None = None,
) -> PdfOutcome:
    started = time.perf_counter()
    sha = hashlib.sha256(data).hexdigest()
    source_id = f"{sha}:{settings.render_dpi}:{'L' if settings.image_grayscale else 'RGB'}"
    raw = await asyncio.to_thread(
        load_pdf,
        data,
        max_pages=settings.max_pages,
        max_mb=settings.max_file_mb,
        dpi=settings.render_dpi,
        grayscale=settings.image_grayscale,
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
    gp = await process_preprocessed(
        pre,
        file_name=file_name,
        physical_pages=len(raw),
        kind=pdf_type(raw),
        provider=provider,
        settings=settings,
        fallback=fallback,
        started=started,
        source_id=source_id,
    )
    full_text = "\n".join(p.text for p in raw)
    apply_text_evidence(gp, text_evidence(full_text))
    own = (gp.so_gp.value or "").upper()
    refs = [r for r in refs_from_text(full_text) if r.so_gp != own]
    return PdfOutcome(gp=gp, sha256=sha, text_refs=refs)
