from app.pipeline.pdf import RawPage, text_layer_ok
from app.pipeline.preprocess import estimate_skew, find_gutter, preprocess
from tests.helpers import A3_LANDSCAPE, A4, GP_2016_P1, blank_page, cover_page, spread, text_page


def _raw(i: int, img: object, size: tuple[int, int], text: str = "") -> RawPage:
    return RawPage(index=i, width_pt=size[0], height_pt=size[1], text=text, image=img)  # type: ignore[arg-type]


def test_text_layer_ok() -> None:
    assert text_layer_ok(GP_2016_P1)
    assert not text_layer_ok("2")
    assert not text_layer_ok("abc " * 100)  # không có dấu tiếng Việt
    assert not text_layer_ok("�" * 300)


def test_a3_split_drops_blank_cover_and_reorders_booklet() -> None:
    sheet1 = spread(blank_page(), cover_page())  # [trang 4 trắng | bìa]
    sheet2 = spread(text_page(lines=30), text_page(lines=20))  # [trang 1 | trang 2]
    res = preprocess([_raw(1, sheet1, A3_LANDSCAPE), _raw(2, sheet2, A3_LANDSCAPE)])
    assert res.spreads == 2
    assert res.reordered
    assert [p.source for p in res.pages] == ["2L", "2R"]
    assert [p.page_no for p in res.pages] == [1, 2]
    assert sorted(p.kind for p in res.dropped) == ["blank", "cover"]


def test_a3_booklet_with_content_on_back() -> None:
    sheet1 = spread(text_page(lines=12), cover_page())  # [trang 3 ngắn | bìa]
    sheet2 = spread(text_page(), text_page())
    res = preprocess([_raw(1, sheet1, A3_LANDSCAPE), _raw(2, sheet2, A3_LANDSCAPE)])
    assert [p.source for p in res.pages] == ["2L", "2R", "1L"]


def test_landscape_without_gutter_is_not_split() -> None:
    img = text_page(w=1200, h=850, bar_w=0.76)  # dòng chữ chạy ngang qua giữa
    assert find_gutter(img) is None
    res = preprocess([_raw(1, img, A3_LANDSCAPE)])
    assert res.spreads == 0
    assert [p.source for p in res.pages] == ["1"]


def test_short_single_page_is_kept() -> None:
    # Trang ký cuối chỉ vài dòng: ít mực như bìa nhưng không được bỏ
    res = preprocess([_raw(1, text_page(), A4), _raw(2, text_page(lines=3), A4), _raw(3, blank_page(), A4)])
    assert [p.source for p in res.pages] == ["1", "2"]
    assert [p.kind for p in res.dropped] == ["blank"]


def test_deskew_estimates_rotation() -> None:
    img = text_page().rotate(2.0, fillcolor="white", expand=False)
    assert abs(estimate_skew(img) - (-2.0)) <= 0.5
    res = preprocess([_raw(1, img, A4)])
    assert abs(res.pages[0].skew_deg + 2.0) <= 0.5


def test_text_pages_are_not_deskewed_or_dropped() -> None:
    res = preprocess([_raw(1, None, A4, GP_2016_P1)])
    assert res.pages[0].kind == "content"
    assert res.pages[0].text == GP_2016_P1


def test_short_page_on_later_sheet_kept_and_duplicate_sheet_dropped() -> None:
    """Scan 2011 Bình Thuận: tờ 1 = [trắng | bìa], tờ 2 = [trang 1 | trang 2 ít chữ], tờ 3 trùng tờ 2."""
    p1, p2 = text_page(lines=30), text_page(lines=6, bar_w=0.5)
    sheets = [spread(blank_page(), cover_page()), spread(p1, p2), spread(p1, p2)]
    raw = [RawPage(i + 1, *A3_LANDSCAPE, image=img) for i, img in enumerate(sheets)]
    res = preprocess(raw, do_deskew=False)
    assert [p.source for p in res.pages] == ["2L", "2R"]
    assert sorted(p.kind for p in res.dropped) == ["blank", "cover", "duplicate", "duplicate"]
