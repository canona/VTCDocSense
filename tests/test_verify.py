from datetime import date

from app.models.schema import Confidence, F, GiayPhep, GpThayThe, LoaiVanBan, Meta, PdfType
from app.pipeline.verify import (
    CrossDoc,
    GpRef,
    apply_text_evidence,
    crosscheck,
    refs_from_gp,
    refs_from_text,
    text_evidence,
)


def _gp(so: str | None, ngay: date | None, conf: Confidence = Confidence.medium) -> GiayPhep:
    return GiayPhep(
        loai_van_ban=LoaiVanBan.GP_HOAT_DONG_LUAT_2016,
        so_gp=F(value=so, confidence=conf),
        ngay_cap=F(value=ngay, confidence=conf),
        meta=Meta(file_name="x.pdf", pages=1, pdf_type=PdfType.SCAN),
    )


def test_refs_from_text_handles_line_breaks() -> None:
    text = (
        "báo chí in số 894/GP-BTTTT ngày 13 tháng 6 năm 2011 và Giấy phép hoạt động \r\n"
        "báo chí điện tử số 201/GP-BTTTT ngày 29 \r\ntháng 5 năm 2015"
    )
    assert refs_from_text(text) == [
        GpRef("894/GP-BTTTT", date(2011, 6, 13)),
        GpRef("201/GP-BTTTT", date(2015, 5, 29)),
    ]


def test_signature_and_sao_y_evidence() -> None:
    ev = text_evidence(
        "Thời gian ký: 05.11.2021 10:00\nSAO Y; Bộ thông tin và Truyền thông; 16/11/2022 16:34:54"
    )
    assert ev.sign_date == date(2021, 11, 5) and ev.sao_y_date == date(2022, 11, 16)
    gp = _gp("1/GP-BTTTT", date(2021, 11, 5))
    apply_text_evidence(gp, ev)
    assert gp.ngay_cap.confidence == Confidence.high and gp.ngay_cap.verified_by == "chu_ky_so"
    late = _gp("1/GP-BTTTT", date(2023, 1, 1), Confidence.high)
    apply_text_evidence(late, text_evidence("SAO Y; Bộ TTTT; 16/11/2022 16:34:54"))
    assert late.ngay_cap.confidence == Confidence.low


def test_crosscheck_upgrades_old_gp() -> None:
    old = _gp("894/GP-BTTTT", date(2011, 6, 13))
    new = _gp("553/GP-BTTTT", date(2021, 9, 1))
    new.gp_duoc_thay_the = [GpThayThe(so_gp="894/GP-BTTTT", ngay="13/06/2011")]
    docs = [
        CrossDoc("old", "GP 2011.pdf", old, refs_from_gp(old)),
        CrossDoc("new", "GP 2021.pdf", new, refs_from_gp(new)),
    ]
    assert crosscheck(docs) == {"old"}
    assert old.so_gp.confidence == Confidence.high
    assert old.ngay_cap.verified_by == "doi_chieu:GP 2021.pdf"
    assert crosscheck(docs) == set()  # chạy lại không đổi gì


def test_crosscheck_date_mismatch_flags_low() -> None:
    old = _gp("894/GP-BTTTT", date(2011, 6, 18), Confidence.high)
    docs = [
        CrossDoc("old", "a.pdf", old, []),
        CrossDoc("new", "b.pdf", _gp(None, None), [GpRef("894/GP-BTTTT", date(2011, 6, 13))]),
    ]
    assert crosscheck(docs) == {"old"}
    assert old.ngay_cap.confidence == Confidence.low and "13/06/2011" in (old.ngay_cap.note or "")
