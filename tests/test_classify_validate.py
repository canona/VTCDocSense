from datetime import date

from app.models.schema import Confidence, GiayPhepCore, LoaiVanBan, Meta, PdfType
from app.pipeline.classify import classify_by_rules
from app.pipeline.preprocess import LogicalPage
from app.pipeline.validate import parse_date, validate
from tests.helpers import CHUYEN_TRANG, GP_1989, GP_2016_P1, GP_2016_P2, TO_KHAI, field, sample_extraction


def _pages(*texts: str) -> list[LogicalPage]:
    return [LogicalPage(page_no=i, source=str(i), text=t) for i, t in enumerate(texts, start=1)]


def test_classify_rules() -> None:
    c = classify_by_rules(_pages(GP_2016_P1, GP_2016_P2))
    assert c is not None and c.loai == LoaiVanBan.GP_HOAT_DONG_LUAT_2016
    assert c.title == "GIẤY PHÉP HOẠT ĐỘNG BÁO IN VÀ BÁO ĐIỆN TỬ"
    c = classify_by_rules(_pages(CHUYEN_TRANG))
    assert c is not None and c.loai == LoaiVanBan.GP_MO_CHUYEN_TRANG
    c = classify_by_rules(_pages(GP_1989))
    assert c is not None and c.loai == LoaiVanBan.GP_HOAT_DONG_LUAT_1989
    c = classify_by_rules(_pages(TO_KHAI))
    assert c is not None and c.loai == LoaiVanBan.KHAC


def test_classify_without_text_defers_to_model() -> None:
    assert classify_by_rules([LogicalPage(page_no=1, source="1")]) is None


def _meta() -> Meta:
    return Meta(file_name="x.pdf", pages=3, pdf_type=PdfType.TEXT)


def test_validate_clean_result() -> None:
    gp = validate(GiayPhepCore.model_validate(sample_extraction()), _meta(), today=date(2026, 10, 6))
    assert gp.ngay_cap.value == date(2021, 9, 29)
    assert gp.so_gp.value == "635/GP-BTTTT"
    assert not gp.needs_review, gp.review_reasons
    assert gp.schema_version == "v1"


def test_validate_flags_problems() -> None:
    data = sample_extraction(
        so_gp=field("635 / GP-BTT"),
        ngay_cap=field("31/02/2021"),
        nguoi_ky=field("Nguyễn Văn A"),
    )
    data["tru_so"]["email"] = field("toasoan@@gmail")  # type: ignore[index]
    gp = validate(GiayPhepCore.model_validate(data), _meta(), today=date(2026, 10, 6))
    assert gp.so_gp.value == "635/GP-BTT"
    assert gp.so_gp.confidence == Confidence.low
    assert gp.ngay_cap.value is None and gp.ngay_cap.confidence == Confidence.low
    assert gp.nguoi_ky.confidence == Confidence.medium
    assert gp.tru_so.email.confidence == Confidence.medium
    assert gp.needs_review
    assert any("Số GP" in r for r in gp.review_reasons)
    assert any("Ngày cấp" in r for r in gp.review_reasons)


def test_validate_future_date_and_missing_number() -> None:
    gp = validate(
        GiayPhepCore.model_validate(
            sample_extraction(so_gp=field(None, note="chưa điền số"), ngay_cap=field("01/01/2030"))
        ),
        _meta(),
        today=date(2026, 10, 6),
    )
    assert gp.ngay_cap.confidence == Confidence.low
    assert "Thiếu số GP" in gp.review_reasons


def test_khac_needs_review() -> None:
    gp = validate(GiayPhepCore(loai_van_ban=LoaiVanBan.KHAC), _meta())
    assert gp.needs_review
    assert gp.review_reasons == ["Không nhận diện được mẫu giấy phép"]


def test_parse_date() -> None:
    assert parse_date("5/1/2023") == date(2023, 1, 5)
    assert parse_date("05.01.2023") == date(2023, 1, 5)
    assert parse_date("2023-01-05") is None
    assert parse_date("31/02/2023") is None
