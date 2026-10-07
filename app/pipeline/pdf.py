"""Đọc PDF bằng pypdfium2 (Apache-2.0/BSD): lớp chữ + render ảnh.

Lưu ý: số GP/ngày trên bản điện tử thường nằm trong hình hiển thị của chữ ký số (form widget),
không có trong lớp chữ -> phải `init_forms()` và render với `may_draw_forms=True`.
"""

import re
from dataclasses import dataclass, field

import pypdfium2 as pdfium
from PIL import Image

MM_PER_PT = 25.4 / 72

_VI_CHARS = set(
    "àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ"
    "ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ"
)
MIN_TEXT_CHARS = 200


class PdfError(Exception):
    """PDF không xử lý được. `code`: encrypted | corrupt | too_many_pages | too_large | empty."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class RawPage:
    index: int  # số trang vật lý, bắt đầu từ 1
    width_pt: float
    height_pt: float
    text: str = ""
    image: Image.Image | None = field(default=None, repr=False)

    @property
    def landscape_ratio(self) -> float:
        return self.width_pt / self.height_pt if self.height_pt else 1.0

    @property
    def has_good_text(self) -> bool:
        return text_layer_ok(self.text)


def text_layer_ok(text: str) -> bool:
    """Lớp chữ đủ dài, ít ký tự rác và có dấu tiếng Việt."""
    t = re.sub(r"\s+", "", text)
    if len(t) < MIN_TEXT_CHARS:
        return False
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return False
    junk = sum(1 for c in t if c == "�" or "" <= c <= "" or not c.isprintable())
    vi = sum(1 for c in letters if c in _VI_CHARS)
    return junk / len(t) < 0.02 and vi / len(letters) > 0.05


def inspect_pdf(data: bytes) -> tuple[int, int]:
    """Đọc nhanh (không render): (số trang, số trang có lớp chữ tốt). Dùng để ước tính chi phí."""
    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise PdfError("corrupt", f"Không đọc được PDF: {e}") from e
    try:
        good = 0
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            good += text_layer_ok(tp.get_text_range())
            tp.close()
            page.close()
        return len(doc), good
    finally:
        doc.close()


def load_pdf(
    data: bytes,
    *,
    max_pages: int,
    max_mb: int,
    dpi: int = 150,
    grayscale: bool = True,
    render_all: bool = False,
) -> list[RawPage]:
    if len(data) > max_mb * 1024 * 1024:
        raise PdfError("too_large", f"File vượt quá {max_mb}MB")
    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        msg = str(e).lower()
        if "password" in msg or "security" in msg:
            raise PdfError("encrypted", "PDF có mật khẩu/mã hóa") from e
        raise PdfError("corrupt", f"Không đọc được PDF: {e}") from e
    try:
        n = len(doc)
        if n == 0:
            raise PdfError("empty", "PDF không có trang nào")
        if n > max_pages:
            raise PdfError("too_many_pages", f"PDF có {n} trang, vượt giới hạn {max_pages}")
        doc.init_forms()
        pages: list[RawPage] = []
        for i in range(n):
            page = doc[i]
            w, h = page.get_size()
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            # Trang có lớp chữ tốt chỉ cần ảnh trang 1 (để đọc số/ngày trong chữ ký số) -> tiết kiệm RAM
            image = None
            if render_all or i == 0 or not text_layer_ok(text):
                image = page.render(scale=dpi / 72, may_draw_forms=True).to_pil()
                image = image.convert("L" if grayscale else "RGB")
            pages.append(RawPage(index=i + 1, width_pt=w, height_pt=h, text=text, image=image))
            textpage.close()
            page.close()
        return pages
    finally:
        doc.close()
