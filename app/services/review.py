"""Rà soát: nhóm trường cho form, áp dụng chỉnh sửa -> bản trích xuất mới + field_reviews + audit."""

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, DocStatus, Document, Extraction, FieldReview
from app.models.schema import SCHEMA_VERSION, GiayPhep
from app.pipeline.run import CRITICAL_FIELDS
from app.pipeline.validate import FIELD_LABELS, parse_date

FIELD_GROUPS: list[tuple[str, list[str]]] = [
    (
        "Giấy phép",
        [
            "so_gp",
            "ngay_cap",
            "co_quan_cap",
            "nguoi_ky",
            "chuc_vu_nguoi_ky",
            "can_cu_gp_goc",
            "van_ban_de_nghi",
        ],
    ),
    (
        "Cơ quan chủ quản",
        [
            "co_quan_chu_quan.ten",
            "co_quan_chu_quan.dia_chi",
            "co_quan_chu_quan.dien_thoai",
            "co_quan_chu_quan.fax",
        ],
    ),
    (
        "Cơ quan báo chí",
        [
            "co_quan_bao_chi.ten",
            "co_quan_bao_chi.loai_hinh_dien_tu",
            "co_quan_bao_chi.ten_chuyen_trang",
            "ton_chi_muc_dich",
            "doi_tuong_phuc_vu",
            "pham_vi_phat_hanh",
            "phuong_thuc_phat_hanh",
        ],
    ),
    ("Trụ sở", ["tru_so.dia_chi", "tru_so.dien_thoai", "tru_so.fax", "tru_so.email", "tru_so.website"]),
    ("Hiệu lực", ["hieu_luc"]),
]
LONG_FIELDS = {"ton_chi_muc_dich", "doi_tuong_phuc_vu", "can_cu_gp_goc", "van_ban_de_nghi", "hieu_luc"}
AN_PHAM_KEYS = [
    "loai_hinh",
    "cap",
    "ten_goi",
    "ngon_ngu",
    "ky_han",
    "thoi_gian_phat_hanh",
    "khuon_kho",
    "so_trang",
    "so_luong",
    "noi_in",
    "ten_mien",
    "isp",
]
LIST_SPECS = {  # tên form -> (khóa JSON, các cột)
    "ap": ("an_pham", AN_PHAM_KEYS),
    "ld": ("lanh_dao", ["chuc_vu", "ho_ten"]),
    "tt": ("gp_duoc_thay_the", ["so_gp", "ngay"]),
}


def get_path(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
    *parents, last = path.split(".")
    cur = data
    for part in parents:
        cur = cur.setdefault(part, {})
    cur[last] = value


def display_value(path: str, value: Any) -> str:
    if value is None:
        return ""
    if path == "ngay_cap" and isinstance(value, str) and len(value) == 10 and value[4] == "-":
        y, m, d = value.split("-")
        return f"{d}/{m}/{y}"
    return str(value)


def sync_summary(doc: Document, data: dict[str, Any]) -> None:
    """Cập nhật cột tóm tắt của document từ bản trích xuất (JSON schema v1)."""
    so = get_path(data, "so_gp") or {}
    ngay = get_path(data, "ngay_cap") or {}
    doc.so_gp = so.get("value")
    nv = ngay.get("value")
    doc.ngay_cap = parse_date(display_value("ngay_cap", nv)) if nv else None
    doc.summary = {
        p: {"v": (get_path(data, p) or {}).get("value"), "c": (get_path(data, p) or {}).get("confidence")}
        for p in CRITICAL_FIELDS
    }
    doc.loai_van_ban = data.get("loai_van_ban")


@dataclass
class ReviewResult:
    changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    extraction: Extraction | None = None


def _parse_lists(form: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for prefix, (key, cols) in LIST_SPECS.items():
        rows: dict[int, dict[str, Any]] = {}
        for name, value in form.items():
            parts = name.split("-", 2)
            if len(parts) == 3 and parts[0] == prefix and parts[1].isdigit() and parts[2] in cols:
                rows.setdefault(int(parts[1]), {})[parts[2]] = value.strip() or None
        out[key] = [
            {c: r.get(c) for c in cols} for _, r in sorted(rows.items()) if any(v for v in r.values())
        ]
    return out


async def apply_review(
    session: AsyncSession,
    doc: Document,
    ext: Extraction,
    form: dict[str, str],
    user_email: str,
    action: str,
) -> ReviewResult:
    """action: save | approve | reject. Chỉ tạo bản trích xuất mới khi có thay đổi/xác nhận."""
    res = ReviewResult()
    data = copy.deepcopy(ext.data)
    tag = f"ra_soat:{user_email}"
    reviews: list[tuple[str, Any, Any]] = []

    for path in FIELD_LABELS:
        name = f"f.{path}"
        if name not in form:
            continue
        old = get_path(data, path) or {}
        raw = form[name].strip()
        new_value: Any = raw or None
        if path == "ngay_cap" and raw:
            d = parse_date(raw)
            if d is None:
                res.errors.append(f"Ngày cấp không hợp lệ: '{raw}' (dạng dd/mm/yyyy)")
                continue
            new_value = d.isoformat()
        confirmed = form.get(f"c.{path}") == "1"
        if new_value != old.get("value") or (confirmed and old.get("confidence") != "high"):
            new = {**old, "value": new_value, "confidence": "high", "verified_by": tag}
            _set_path(data, path, new)
            reviews.append(
                (
                    path,
                    {"value": old.get("value"), "confidence": old.get("confidence")},
                    {"value": new_value, "confidence": "high"},
                )
            )

    if "ten_loai_giay_phep" in form:
        v = form["ten_loai_giay_phep"].strip() or None
        if v != data.get("ten_loai_giay_phep"):
            reviews.append(("ten_loai_giay_phep", data.get("ten_loai_giay_phep"), v))
            data["ten_loai_giay_phep"] = v
    if form.get("loai_van_ban") and form["loai_van_ban"] != data.get("loai_van_ban"):
        reviews.append(("loai_van_ban", data.get("loai_van_ban"), form["loai_van_ban"]))
        data["loai_van_ban"] = form["loai_van_ban"]
    if form.get("lists") == "1":  # form có gửi các bảng
        for key, rows in _parse_lists(form).items():
            if rows != (data.get(key) or []):
                reviews.append((key, data.get(key) or [], rows))
                data[key] = rows

    try:
        GiayPhep.model_validate(data)
    except ValidationError as e:
        res.errors += [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:5]]
    if res.errors:
        return res

    if reviews:
        new_ext = Extraction(
            id=uuid.uuid4(),
            document_id=doc.id,
            version=ext.version + 1,
            data=data,
            schema_version=SCHEMA_VERSION,
            provider=ext.provider,
            model=ext.model,
            needs_review=ext.needs_review,
            created_by=user_email,
        )
        session.add(new_ext)
        for path, old_v, new_v in reviews:
            session.add(
                FieldReview(
                    document_id=doc.id,
                    extraction_id=new_ext.id,
                    field_path=path,
                    old_value=old_v,
                    new_value=new_v,
                    user_email=user_email,
                )
            )
        sync_summary(doc, data)
        res.extraction = new_ext
        res.changed = [p for p, _, _ in reviews]

    if action == "approve":
        doc.status, doc.reviewed_by = DocStatus.approved, user_email
    elif action == "reject":
        doc.status, doc.reviewed_by = DocStatus.rejected, user_email
    if reviews or action != "save":
        session.add(
            AuditLog(
                tenant_id=doc.tenant_id,
                actor=user_email,
                action=f"document.{action}",
                target_type="document",
                target_id=str(doc.id),
                details={
                    "changed": res.changed,
                    "version": res.extraction.version if res.extraction else None,
                },
            )
        )
    await session.commit()
    return res
