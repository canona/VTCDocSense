"""Schema output `v1` của Giấy phép báo chí.

- `GiayPhepCore`: phần model bóc tách (ngày dạng chuỗi `dd/mm/yyyy` đúng như văn bản).
- `GiayPhep`: kết quả cuối, thêm `meta`, `needs_review`, `ngay_cap` kiểu `date`.
"""

import copy
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = "v1"


class Confidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class LoaiVanBan(StrEnum):
    GP_HOAT_DONG_LUAT_1989 = "GP_HOAT_DONG_LUAT_1989"
    GP_HOAT_DONG_LUAT_2016 = "GP_HOAT_DONG_LUAT_2016"
    GP_MO_CHUYEN_TRANG = "GP_MO_CHUYEN_TRANG"
    KHAC = "KHAC"


class LoaiHinh(StrEnum):
    BAO_IN = "BAO_IN"
    BAO_DIEN_TU = "BAO_DIEN_TU"
    CHUYEN_TRANG = "CHUYEN_TRANG"


class ChucVu(StrEnum):
    TONG_BIEN_TAP = "TONG_BIEN_TAP"
    PHO_TONG_BIEN_TAP = "PHO_TONG_BIEN_TAP"


class PdfType(StrEnum):
    TEXT = "TEXT"
    SCAN = "SCAN"
    MIXED = "MIXED"


class F[T](BaseModel):
    """Field[T]: giá trị kèm độ tin cậy và nguồn."""

    value: T | None = None
    confidence: Confidence = Confidence.high
    note: str | None = None
    source_page: int | None = None
    verified_by: str | None = None


class CoQuanChuQuan(BaseModel):
    ten: F[str] = F()
    dia_chi: F[str] = F()
    dien_thoai: F[str] = F()
    fax: F[str] = F()


class CoQuanBaoChi(BaseModel):
    ten: F[str] = F()
    loai_hinh_dien_tu: F[str] = F()
    ten_chuyen_trang: F[str] = F()


class TruSo(BaseModel):
    dia_chi: F[str] = F()
    dien_thoai: F[str] = F()
    fax: F[str] = F()
    email: F[str] = F()
    website: F[str] = F()


class GpThayThe(BaseModel):
    so_gp: str | None = None
    ngay: str | None = None  # dd/mm/yyyy


class AnPham(BaseModel):
    loai_hinh: LoaiHinh
    cap: str | None = None  # "Ấn phẩm chính" / "Ấn phẩm khác"
    ten_goi: str | None = None
    ngon_ngu: str | None = None
    ky_han: str | None = None
    thoi_gian_phat_hanh: str | None = None
    khuon_kho: str | None = None
    so_trang: str | None = None
    so_luong: str | None = None
    noi_in: str | None = None
    ten_mien: str | None = None
    isp: str | None = None


class LanhDao(BaseModel):
    chuc_vu: ChucVu
    ho_ten: str


class GiayPhepCore(BaseModel):
    loai_van_ban: LoaiVanBan
    ten_loai_giay_phep: str | None = None
    so_gp: F[str] = F()
    ngay_cap: F[str] = F()
    co_quan_cap: F[str] = F()
    nguoi_ky: F[str] = F()
    chuc_vu_nguoi_ky: F[str] = F()
    can_cu_gp_goc: F[str] = F()
    van_ban_de_nghi: F[str] = F()
    co_quan_chu_quan: CoQuanChuQuan = CoQuanChuQuan()
    co_quan_bao_chi: CoQuanBaoChi = CoQuanBaoChi()
    ton_chi_muc_dich: F[str] = F()
    doi_tuong_phuc_vu: F[str] = F()
    pham_vi_phat_hanh: F[str] = F()
    phuong_thuc_phat_hanh: F[str] = F()
    tru_so: TruSo = TruSo()
    hieu_luc: F[str] = F()
    gp_duoc_thay_the: list[GpThayThe] = Field(default_factory=list)
    an_pham: list[AnPham] = Field(default_factory=list)
    lanh_dao: list[LanhDao] = Field(default_factory=list)


class Meta(BaseModel):
    file_name: str
    pages: int
    logical_pages: int = 0
    pdf_type: PdfType
    provider: str | None = None
    model: str | None = None
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    classified_by: str | None = None  # "rule" | "model"


class GiayPhep(GiayPhepCore):
    schema_version: str = SCHEMA_VERSION
    ngay_cap: F[date] = F()  # type: ignore[assignment]
    meta: Meta
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


# ---------- JSON Schema cho structured output ----------

# Model không được tự điền các khóa này (pipeline điền sau)
_LLM_EXCLUDED_PROPS = {"verified_by"}


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return _inline_refs(copy.deepcopy(defs[name]), defs)
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _strictify(node: Any) -> Any:
    """Chuẩn strict của OpenAI: mọi object `additionalProperties: false`, mọi khóa đều required,
    `anyOf[X, null]` -> `type: [X, "null"]`; bỏ title/default."""
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    if not isinstance(node, dict):
        return node
    node = {k: v for k, v in node.items() if k not in ("title", "default")}
    any_of = node.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
        other = next(x for x in any_of if x != {"type": "null"})
        if "type" in other and isinstance(other["type"], str) and "enum" not in other:
            del node["anyOf"]
            node.update(other)
            node["type"] = [other["type"], "null"]
    if node.get("type") == "object" and "properties" in node:
        props = {k: v for k, v in node["properties"].items() if k not in _LLM_EXCLUDED_PROPS}
        node["properties"] = props
        node["required"] = list(props)
        node["additionalProperties"] = False
    return {k: _strictify(v) for k, v in node.items()}


def llm_json_schema(model: type[BaseModel] = GiayPhepCore) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    return _strictify(_inline_refs(raw, defs))  # type: ignore[no-any-return]


def public_json_schema() -> dict[str, Any]:
    """Schema đầy đủ của output (phục vụ `GET /v1/schema` ở M2)."""
    return GiayPhep.model_json_schema()
