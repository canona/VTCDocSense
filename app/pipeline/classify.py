"""Phân loại mẫu văn bản bằng rule trên lớp chữ; trả None nếu không đủ chữ (fallback hỏi model).

Phân loại theo luật làm căn cứ: GP hoạt động báo chí *điện tử* cấp theo Luật 1989
(vd Báo Ấp Bắc 2012) cũng xếp vào GP_HOAT_DONG_LUAT_1989.
"""

import re
from dataclasses import dataclass

from pydantic import BaseModel

from app.models.schema import LoaiVanBan
from app.pipeline.pdf import text_layer_ok
from app.pipeline.preprocess import LogicalPage


@dataclass
class Classification:
    loai: LoaiVanBan
    title: str | None
    by: str  # "rule" | "model"
    reason: str | None = None


class ClassifyOut(BaseModel):
    """Schema cho model khi không có lớp chữ."""

    loai_van_ban: LoaiVanBan
    ten_loai_giay_phep: str | None = None
    ly_do: str | None = None


_TITLE_RE = re.compile(r"^GIẤY PHÉP\b.*$", re.MULTILINE)


def _norm(text: str) -> str:
    return "\n".join(" ".join(line.split()) for line in text.upper().splitlines() if line.strip())


def _title(norm: str) -> str | None:
    m = _TITLE_RE.search(norm)
    return m.group(0).strip() if m else None


def classify_by_rules(pages: list[LogicalPage]) -> Classification | None:
    text = "\n".join(p.text or "" for p in pages[:2])
    if not text_layer_ok(text):
        return None
    norm = _norm(text)
    flat = norm.replace("\n", " ")
    title = _title(norm)
    if not title:
        return Classification(LoaiVanBan.KHAC, None, "rule", "không có tiêu đề 'GIẤY PHÉP ...'")
    # Văn bản cấp phép thật có phần quyết định; tờ khai/đề án/phiếu trình thì không
    if "QUYẾT ĐỊNH" not in flat and "CẤP GIẤY PHÉP" not in flat:
        return Classification(LoaiVanBan.KHAC, title, "rule", "không có phần 'QUYẾT ĐỊNH'")
    if "MỞ CHUYÊN TRANG" in title:
        return Classification(LoaiVanBan.GP_MO_CHUYEN_TRANG, title, "rule")
    if "HOẠT ĐỘNG BÁO" not in title:
        return Classification(LoaiVanBan.KHAC, title, "rule", "tiêu đề không phải GP hoạt động/chuyên trang")
    law2016 = "05 THÁNG 4 NĂM 2016" in flat or "LUẬT BÁO CHÍ NĂM 2016" in flat
    law1989 = "28 THÁNG 12 NĂM 1989" in flat
    if law2016 and not law1989:
        return Classification(LoaiVanBan.GP_HOAT_DONG_LUAT_2016, title, "rule")
    if law1989 and not law2016:
        return Classification(LoaiVanBan.GP_HOAT_DONG_LUAT_1989, title, "rule")
    # Không thấy căn cứ luật rõ ràng -> đoán theo tiêu đề
    if "BÁO CHÍ IN" in title:
        return Classification(LoaiVanBan.GP_HOAT_DONG_LUAT_1989, title, "rule", "đoán theo tiêu đề")
    if "BÁO IN" in title or "BÁO ĐIỆN TỬ" in title:
        return Classification(LoaiVanBan.GP_HOAT_DONG_LUAT_2016, title, "rule", "đoán theo tiêu đề")
    return None
