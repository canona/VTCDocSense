"""Xác minh không cần LLM: thời gian ký số / dòng SAO Y trong lớp chữ và đối chiếu chéo GP cùng thư mục.

- "Thời gian ký: dd/mm/yyyy" (chữ ký số của người ký GP) thường trùng ngày cấp.
- "SAO Y; <cơ quan>; dd/mm/yyyy hh:mm:ss" là ngày sao y -> ngày cấp không thể sau ngày này.
- GP mới hay trích dẫn "số X/GP-... ngày D tháng M năm Y" của GP cũ; nếu GP cũ cùng thư mục có
  số + ngày khớp thì nâng độ tin cậy (số/ngày GP cũ thường viết tay).
"""

import re
from dataclasses import dataclass
from datetime import date

from app.models.schema import Confidence, GiayPhep
from app.pipeline.validate import normalize_so_gp, parse_date

_SIGN_RE = re.compile(r"Thời\s+gian\s+ký\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})", re.IGNORECASE)
_SAO_Y_RE = re.compile(r"SAO\s+Y\s*;[^;\n]*;\s*(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
_REF_RE = re.compile(
    r"số\s+(\d+\s*/\s*GP\s*-\s*[A-ZĐ]+)\s+ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})",
    re.IGNORECASE,
)
_REF_SHORT_RE = re.compile(r"(\d+\s*/\s*GP\s*-\s*[A-ZĐ]+)\s*,?\s*ngày\s+(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")


@dataclass(frozen=True)
class GpRef:
    so_gp: str
    ngay: date


@dataclass
class TextEvidence:
    sign_date: date | None = None
    sao_y_date: date | None = None


def _norm_date(s: str) -> date | None:
    return parse_date(s.replace(".", "/").replace("-", "/"))


def text_evidence(text: str) -> TextEvidence:
    sign = _SIGN_RE.search(text)
    sao = _SAO_Y_RE.search(text)
    return TextEvidence(
        sign_date=_norm_date(sign.group(1)) if sign else None,
        sao_y_date=_norm_date(sao.group(1)) if sao else None,
    )


def _mk_ref(so: str, d: str, m: str, y: str) -> GpRef | None:
    try:
        ngay = date(int(y), int(m), int(d))
    except ValueError:
        return None
    so_n = normalize_so_gp(so)
    return GpRef(so_n.upper(), ngay) if so_n else None


def refs_from_text(text: str) -> list[GpRef]:
    flat = " ".join(text.split())
    out = [_mk_ref(*m.groups()) for m in _REF_RE.finditer(flat)]
    out += [_mk_ref(*m.groups()) for m in _REF_SHORT_RE.finditer(flat)]
    return list(dict.fromkeys(r for r in out if r))


def refs_from_gp(gp: GiayPhep) -> list[GpRef]:
    """GP cũ được trích dẫn trong kết quả model (gp_duoc_thay_the, can_cu_gp_goc)."""
    out: list[GpRef] = []
    for t in gp.gp_duoc_thay_the:
        d = parse_date(t.ngay)
        so = normalize_so_gp(t.so_gp)
        if d and so:
            out.append(GpRef(so.upper(), d))
    if gp.can_cu_gp_goc.value:
        out += refs_from_text(gp.can_cu_gp_goc.value)
    return list(dict.fromkeys(out))


def apply_text_evidence(gp: GiayPhep, ev: TextEvidence) -> None:
    f = gp.ngay_cap
    if ev.sign_date:
        if f.value == ev.sign_date:
            f.confidence = Confidence.high
            f.verified_by = "chu_ky_so"
        elif f.value is None:
            f.value, f.confidence = ev.sign_date, Confidence.medium
            f.note = "lấy từ thời gian ký số trong lớp chữ"
        else:
            f.note = f"khác thời gian ký số ({ev.sign_date:%d/%m/%Y})"
            if f.confidence == Confidence.high:
                f.confidence = Confidence.medium
    if ev.sao_y_date and f.value and f.value > ev.sao_y_date:
        f.confidence = Confidence.low
        f.note = f"ngày cấp sau ngày sao y ({ev.sao_y_date:%d/%m/%Y})"


@dataclass
class CrossDoc:
    key: str  # id document / tên file
    label: str  # tên hiển thị cho verified_by
    gp: GiayPhep
    refs: list[GpRef]


def crosscheck(docs: list[CrossDoc]) -> set[str]:
    """Đối chiếu trong cùng thư mục; trả về tập `key` của document có thay đổi."""
    changed: set[str] = set()
    for target in docs:
        so = normalize_so_gp(target.gp.so_gp.value)
        if not so:
            continue
        for src in docs:
            if src is target:
                continue
            for ref in src.refs:
                if ref.so_gp != so.upper():
                    continue
                tag = f"doi_chieu:{src.label}"
                if target.gp.ngay_cap.value == ref.ngay:
                    for f in (target.gp.so_gp, target.gp.ngay_cap):
                        if f.verified_by != tag:
                            f.confidence, f.verified_by = Confidence.high, tag
                            changed.add(target.key)
                elif target.gp.ngay_cap.value is None:
                    f = target.gp.ngay_cap
                    f.value, f.confidence = ref.ngay, Confidence.medium
                    f.note, f.verified_by = f"lấy từ trích dẫn trong {src.label}", tag
                    changed.add(target.key)
                else:
                    note = f"{src.label} trích dẫn ngày {ref.ngay:%d/%m/%Y}"
                    if target.gp.ngay_cap.note != note:
                        target.gp.ngay_cap.note = note
                        target.gp.ngay_cap.confidence = Confidence.low
                        changed.add(target.key)
    return changed
