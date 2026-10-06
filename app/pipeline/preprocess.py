"""Tiền xử lý: cắt trang A3 ghép 2 trang A4, bỏ trang trắng/bìa, xoay thẳng, sắp lại thứ tự.

Chỉ dùng Pillow + numpy. Ngưỡng hiệu chỉnh trên bộ mẫu `eval/pdfs` (scan 200 DPI).
"""

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from PIL import Image

from app.pipeline.pdf import RawPage, text_layer_ok

PageKind = Literal["content", "cover", "blank"]

SPREAD_MIN_RATIO = 1.3  # A3/A4 ngang ~ 1.41; Letter ngang 1.29 (măng sét) không cắt
WORK_WIDTH = 600  # px, ảnh thu nhỏ để tính toán
INK_LEVEL = 150  # pixel xám < ngưỡng = mực
INNER_MARGIN = 0.15  # bỏ viền hoa văn khi đo mực
BLANK_MAX_INK = 0.004
COVER_MAX_INK = 0.035
DESKEW_MAX_DEG = 3.0
DESKEW_STEP_DEG = 0.25
DESKEW_MIN_DEG = 0.3


@dataclass
class LogicalPage:
    page_no: int  # số trang logic sau sắp xếp, bắt đầu từ 1
    source: str  # trang vật lý gốc, vd "2" hoặc "1R" (nửa phải trang 1)
    text: str | None = None
    image: Image.Image | None = field(default=None, repr=False)
    kind: PageKind = "content"
    skew_deg: float = 0.0


@dataclass
class PreprocessResult:
    pages: list[LogicalPage]  # chỉ trang nội dung, đã đánh số lại
    dropped: list[LogicalPage]
    spreads: int = 0  # số trang A3 đã cắt
    reordered: bool = False


def _gray_small(img: Image.Image) -> np.ndarray:
    g = img.convert("L")
    if g.width > WORK_WIDTH:
        g = g.resize((WORK_WIDTH, max(1, round(g.height * WORK_WIDTH / g.width))), Image.Resampling.BILINEAR)
    return np.asarray(g, dtype=np.uint8)


def find_gutter(img: Image.Image) -> int | None:
    """Tìm khe giữa 2 trang (cột ít mực nhất ở vùng 40-60% chiều ngang). Trả về x theo ảnh gốc."""
    a = _gray_small(img)
    ink = (a < INK_LEVEL).astype(np.float32)
    h = ink.shape[0]
    cols = ink[int(h * 0.1) : int(h * 0.9)].mean(axis=0)
    w = cols.shape[0]
    lo, hi = int(w * 0.4), int(w * 0.6)
    # làm mượt để bỏ nhiễu đơn lẻ
    k = max(3, w // 100)
    smooth = np.convolve(cols, np.ones(k) / k, mode="same")
    mid = smooth[lo:hi]
    x = int(np.argmin(mid)) + lo
    page_ink = float(np.median(smooth[int(w * 0.1) : int(w * 0.9)]))
    if smooth[x] > max(0.01, 0.35 * page_ink):
        return None
    return int(round(x * img.width / w))


def ink_ratio(img: Image.Image) -> float:
    a = _gray_small(img)
    h, w = a.shape
    my, mx = int(h * INNER_MARGIN), int(w * INNER_MARGIN)
    inner = a[my : h - my, mx : w - mx]
    return float((inner < INK_LEVEL).mean()) if inner.size else 0.0


def page_kind(text: str | None, img: Image.Image | None, *, spread_half: bool = False) -> PageKind:
    """Trang lẻ ít mực có thể là trang ký ngắn -> chỉ coi là bìa khi là nửa tờ A3 ghép."""
    if text and text_layer_ok(text):
        return "content"
    if text:
        t = " ".join(text.upper().split())
        if "GIẤY PHÉP" in t and len(t) < 200 and "QUYẾT ĐỊNH" not in t:
            return "cover"
    if img is None:
        return "blank" if not (text and text.strip()) else "content"
    r = ink_ratio(img)
    if r < BLANK_MAX_INK:
        return "blank"
    if spread_half and r < COVER_MAX_INK:
        return "cover"
    return "content"


def estimate_skew(img: Image.Image) -> float:
    """Góc nghiêng (độ) theo phương pháp projection profile: góc làm phương sai tổng hàng lớn nhất."""
    a = _gray_small(img)
    binary = Image.fromarray(((a < INK_LEVEL) * 255).astype(np.uint8))
    best, best_score = 0.0, -1.0
    steps = int(DESKEW_MAX_DEG / DESKEW_STEP_DEG)
    for i in range(-steps, steps + 1):
        ang = i * DESKEW_STEP_DEG
        rot = np.asarray(binary.rotate(ang, resample=Image.Resampling.NEAREST, fillcolor=0), dtype=np.float32)
        score = float(np.var(rot.sum(axis=1)))
        if score > best_score:
            best, best_score = ang, score
    return best


def deskew(img: Image.Image) -> tuple[Image.Image, float]:
    ang = estimate_skew(img)
    if abs(ang) < DESKEW_MIN_DEG:
        return img, 0.0
    return img.rotate(ang, resample=Image.Resampling.BICUBIC, expand=False, fillcolor="white"), ang


def preprocess(raw_pages: list[RawPage], *, do_deskew: bool = True) -> PreprocessResult:
    items: list[LogicalPage] = []
    sheets: list[tuple[LogicalPage, LogicalPage]] = []
    for rp in raw_pages:
        good_text = text_layer_ok(rp.text)
        img = rp.image
        if not good_text and img is not None and rp.landscape_ratio >= SPREAD_MIN_RATIO:
            gx = find_gutter(img)
            if gx is not None:
                left = LogicalPage(0, f"{rp.index}L", None, img.crop((0, 0, gx, img.height)))
                right = LogicalPage(0, f"{rp.index}R", None, img.crop((gx, 0, img.width, img.height)))
                items += [left, right]
                sheets.append((left, right))
                continue
        items.append(LogicalPage(0, str(rp.index), rp.text if rp.text.strip() else None, img))

    halves = {id(p) for sheet in sheets for p in sheet}
    for p in items:
        p.kind = page_kind(p.text, p.image, spread_half=id(p) in halves)

    reordered = False
    # Sổ gấp 2 tờ A3: tờ 1 = [trang 4 | trang 1], tờ 2 = [trang 2 | trang 3].
    # Chỉ áp dụng khi có dấu hiệu: tờ 1 có nửa trắng/bìa (bản scan ghép A3 điển hình).
    if len(sheets) == 2 and len(items) == 4:
        (s1l, s1r), (s2l, s2r) = sheets
        if s1l.kind != "content" or s1r.kind != "content":
            booklet = [s1r, s2l, s2r, s1l]
            reordered = booklet != items
            items = booklet

    content = [p for p in items if p.kind == "content"]
    dropped = [p for p in items if p.kind != "content"]
    for i, p in enumerate(content, start=1):
        p.page_no = i
        if do_deskew and p.image is not None and not (p.text and text_layer_ok(p.text)):
            p.image, p.skew_deg = deskew(p.image)
    return PreprocessResult(pages=content, dropped=dropped, spreads=len(sheets), reordered=reordered)
