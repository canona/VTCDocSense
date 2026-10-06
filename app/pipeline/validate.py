"""Chuẩn hóa & kiểm tra rule-based; vi phạm -> hạ confidence + ghi note + review_reasons."""

import re
from collections.abc import Iterator
from datetime import date
from functools import lru_cache
from pathlib import Path

from app.models.schema import (
    Confidence,
    F,
    GiayPhep,
    GiayPhepCore,
    LoaiHinh,
    LoaiVanBan,
    Meta,
)

SO_GP_RE = re.compile(r"^\d+/GP-(BTTTT|CBC|BVHTT)$")
DATE_RE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")
PHONE_RE = re.compile(r"^\(?\+?\d[\d .()-]{6,}\d$")
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
DOMAIN_RE = re.compile(r"^(https?://)?(www\.)?([a-z0-9-]+\.)+[a-z]{2,}(/\S*)?$", re.IGNORECASE)
_SPLIT_RE = re.compile(r"\s*[;,]\s*|\s+và\s+")

_SIGNERS_FILE = Path(__file__).parent / "signers.txt"


@lru_cache
def known_signers() -> frozenset[str]:
    lines = _SIGNERS_FILE.read_text(encoding="utf-8").splitlines()
    return frozenset(ln.strip() for ln in lines if ln.strip() and not ln.startswith("#"))


_DOWN = {
    Confidence.high: Confidence.medium,
    Confidence.medium: Confidence.low,
    Confidence.low: Confidence.low,
}

# Nhãn tiếng Việt dùng trong review_reasons/Excel
FIELD_LABELS = {
    "so_gp": "Số GP",
    "ngay_cap": "Ngày cấp",
    "co_quan_cap": "Cơ quan cấp",
    "nguoi_ky": "Người ký",
    "chuc_vu_nguoi_ky": "Chức vụ người ký",
    "can_cu_gp_goc": "Căn cứ GP gốc",
    "van_ban_de_nghi": "Văn bản đề nghị",
    "co_quan_chu_quan.ten": "Cơ quan chủ quản",
    "co_quan_chu_quan.dia_chi": "Địa chỉ CQ chủ quản",
    "co_quan_chu_quan.dien_thoai": "Điện thoại CQ chủ quản",
    "co_quan_chu_quan.fax": "Fax CQ chủ quản",
    "co_quan_bao_chi.ten": "Tên cơ quan báo chí",
    "co_quan_bao_chi.loai_hinh_dien_tu": "Loại hình điện tử",
    "co_quan_bao_chi.ten_chuyen_trang": "Tên chuyên trang",
    "ton_chi_muc_dich": "Tôn chỉ, mục đích",
    "doi_tuong_phuc_vu": "Đối tượng phục vụ",
    "pham_vi_phat_hanh": "Phạm vi phát hành",
    "phuong_thuc_phat_hanh": "Phương thức phát hành",
    "tru_so.dia_chi": "Địa chỉ trụ sở",
    "tru_so.dien_thoai": "Điện thoại trụ sở",
    "tru_so.fax": "Fax trụ sở",
    "tru_so.email": "Email",
    "tru_so.website": "Website",
    "hieu_luc": "Hiệu lực",
}


def iter_fields(gp: GiayPhepCore) -> Iterator[tuple[str, F]]:  # type: ignore[type-arg]
    """Duyệt mọi Field theo thứ tự FIELD_LABELS (khóa dạng 'a.b')."""
    for key in FIELD_LABELS:
        obj: object = gp
        for part in key.split("."):
            obj = getattr(obj, part)
        assert isinstance(obj, F)
        yield key, obj


def _downgrade(f: F, note: str) -> None:  # type: ignore[type-arg]
    f.confidence = _DOWN[f.confidence]
    f.note = f"{f.note}; {note}" if f.note else note


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    m = DATE_RE.match(s.strip())
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def normalize_date_str(s: str | None) -> str | None:
    d = parse_date(s)
    return d.strftime("%d/%m/%Y") if d else s


def normalize_so_gp(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", "", s).replace("–", "-")
    return s or None


def _check_multi(f: F, pattern: re.Pattern[str], what: str) -> None:  # type: ignore[type-arg]
    if not f.value:
        return
    parts = [p for p in _SPLIT_RE.split(f.value.strip().rstrip(".;")) if p]
    if not parts or not all(pattern.match(p) for p in parts):
        _downgrade(f, f"{what} sai định dạng")


def validate(core: GiayPhepCore, meta: Meta, *, today: date | None = None) -> GiayPhep:
    today = today or date.today()
    reasons: list[str] = []
    ngay_raw = core.ngay_cap

    # --- số GP ---
    core.so_gp.value = normalize_so_gp(core.so_gp.value)
    if core.so_gp.value and not SO_GP_RE.match(core.so_gp.value):
        _downgrade(core.so_gp, "số GP không khớp mẫu \\d+/GP-(BTTTT|CBC|BVHTT)")
        core.so_gp.confidence = Confidence.low
    if (
        core.loai_van_ban == LoaiVanBan.GP_MO_CHUYEN_TRANG
        and core.so_gp.value
        and not core.so_gp.value.endswith("/GP-CBC")
    ):
        _downgrade(core.so_gp, "GP chuyên trang thường có số dạng xx/GP-CBC")

    # --- ngày cấp ---
    d = parse_date(ngay_raw.value)
    ngay_note, ngay_conf = ngay_raw.note, ngay_raw.confidence
    if ngay_raw.value and d is None:
        ngay_conf = Confidence.low
        ngay_note = (
            f"{ngay_note}; ngày không hợp lệ: {ngay_raw.value!r}"
            if ngay_note
            else (f"ngày không hợp lệ: {ngay_raw.value!r}")
        )
    elif d and d > today:
        ngay_conf = Confidence.low
        ngay_note = f"{ngay_note}; ngày ở tương lai" if ngay_note else "ngày ở tương lai"

    # --- liên hệ ---
    for f in (
        core.co_quan_chu_quan.dien_thoai,
        core.co_quan_chu_quan.fax,
        core.tru_so.dien_thoai,
        core.tru_so.fax,
    ):
        _check_multi(f, PHONE_RE, "số điện thoại/fax")
    _check_multi(core.tru_so.email, EMAIL_RE, "email")
    _check_multi(core.tru_so.website, DOMAIN_RE, "website")
    for ap in core.an_pham:
        if ap.ten_mien and not all(
            DOMAIN_RE.match(p) for p in _SPLIT_RE.split(ap.ten_mien.strip(" .;")) if p
        ):
            reasons.append(f"Tên miền sai định dạng: {ap.ten_mien}")
    for gp in core.gp_duoc_thay_the:
        gp.so_gp = normalize_so_gp(gp.so_gp)
        gp.ngay = normalize_date_str(gp.ngay)

    # --- người ký ---
    if core.nguoi_ky.value and core.nguoi_ky.value.strip() not in known_signers():
        _downgrade(core.nguoi_ky, "người ký không có trong danh sách đã biết")

    # --- nhất quán theo mẫu ---
    if core.loai_van_ban == LoaiVanBan.GP_MO_CHUYEN_TRANG and not any(
        a.loai_hinh == LoaiHinh.CHUYEN_TRANG for a in core.an_pham
    ):
        reasons.append("GP chuyên trang nhưng không có ấn phẩm loại CHUYEN_TRANG")

    # --- tổng hợp cần rà soát ---
    if core.loai_van_ban == LoaiVanBan.KHAC:
        reasons.append("Không nhận diện được mẫu giấy phép")
    else:
        if not core.so_gp.value:
            reasons.append("Thiếu số GP")
        if not ngay_raw.value:
            reasons.append("Thiếu ngày cấp")
        if ngay_conf == Confidence.low:
            reasons.append(f"Ngày cấp: {ngay_note}")
        for key, f in iter_fields(core):
            if f.confidence == Confidence.low and key != "ngay_cap":
                reasons.append(f"{FIELD_LABELS[key]}: {f.note or 'độ tin cậy thấp'}")

    data = core.model_dump()
    data["ngay_cap"] = {**data["ngay_cap"], "value": d, "confidence": ngay_conf, "note": ngay_note}
    return GiayPhep(**data, meta=meta, needs_review=bool(reasons), review_reasons=reasons)
