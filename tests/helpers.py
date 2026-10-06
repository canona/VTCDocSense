"""Sinh trang giả lập (ảnh scan) và văn bản mẫu cho test pipeline."""

from io import BytesIO

from PIL import Image, ImageDraw

A4 = (595, 842)  # pt
A3_LANDSCAPE = (1190, 842)


def text_page(w: int = 600, h: int = 850, lines: int = 30, bar_w: float = 0.7) -> Image.Image:
    """Trang nội dung: nhiều 'dòng chữ' là các thanh đen ngang."""
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    top, step = int(h * 0.12), int(h * 0.76 / lines)
    for i in range(lines):
        y = top + i * step
        d.rectangle((int(w * 0.12), y, int(w * (0.12 + bar_w)), y + max(2, step // 3)), fill="black")
    return img


def cover_page(w: int = 600, h: int = 850) -> Image.Image:
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((int(w * 0.25), int(h * 0.45), int(w * 0.75), int(h * 0.47)), fill="black")
    d.rectangle((5, 5, w - 5, h - 5), outline="black", width=4)  # viền hoa văn
    return img


def blank_page(w: int = 600, h: int = 850) -> Image.Image:
    img = Image.new("RGB", (w, h), "white")
    ImageDraw.Draw(img).rectangle((5, 5, w - 5, h - 5), outline="black", width=4)
    return img


def spread(left: Image.Image, right: Image.Image, gutter: int = 20) -> Image.Image:
    img = Image.new("RGB", (left.width + right.width + gutter, max(left.height, right.height)), "white")
    img.paste(left, (0, 0))
    img.paste(right, (left.width + gutter, 0))
    return img


def images_to_pdf(images: list[Image.Image], dpi: float = 72.0) -> bytes:
    buf = BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=dpi)
    return buf.getvalue()


GP_2016_P1 = """BỘ THÔNG TIN VÀ TRUYỀN THÔNG CỘNG HOÀ XÃ HỘI CHỦ NGHĨA VIỆT NAM
Số: /GP-BTTTT Hà Nội, ngày tháng 9 năm 2021
GIẤY PHÉP HOẠT ĐỘNG BÁO IN VÀ BÁO ĐIỆN TỬ
BỘ TRƯỞNG BỘ THÔNG TIN VÀ TRUYỀN THÔNG
Căn cứ Luật Báo chí ngày 05 tháng 4 năm 2016;
Căn cứ Nghị định số 48/2022/NĐ-CP quy định chức năng, nhiệm vụ, quyền hạn của Bộ Thông tin và Truyền thông;
Theo đề nghị của Cục trưởng Cục Báo chí,
QUYẾT ĐỊNH:
CẤP GIẤY PHÉP HOẠT ĐỘNG BÁO IN VÀ BÁO ĐIỆN TỬ THEO NHỮNG QUY ĐỊNH SAU:
1. Tên cơ quan chủ quản báo chí: Tỉnh ủy An Giang
2. Tên cơ quan báo chí: Báo An Giang
"""

GP_2016_P2 = """3. Tôn chỉ, mục đích: Tuyên truyền đường lối, chủ trương của Đảng,
chính sách, pháp luật của Nhà nước;
4. Đối tượng phục vụ: Cán bộ, đảng viên, nhân dân trong Tỉnh và bạn đọc quan tâm.
5. Các loại hình: 5.1 Báo in; 5.2 Báo điện tử: tên miền baoangiang.com.vn
6. Trụ sở chính: 399B Hà Hoàng Hổ, phường Mỹ Xuyên, thành phố Long Xuyên, tỉnh An Giang
"""

TO_KHAI = """CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM Độc lập - Tự do - Hạnh phúc
TỜ KHAI ĐỀ NGHỊ CẤP GIẤY PHÉP HOẠT ĐỘNG BÁO ĐIỆN TỬ
Kính gửi: Bộ Thông tin và Truyền thông. Căn cứ Luật Báo chí ngày 05 tháng 4 năm 2016, cơ quan chủ quản
đề nghị cấp giấy phép hoạt động báo điện tử với các nội dung sau đây: tên báo, tôn chỉ mục đích, đối tượng
phục vụ, tên miền, nhân sự dự kiến, phương án tài chính và cam kết thực hiện đúng quy định của pháp luật.
"""

CHUYEN_TRANG = """BỘ THÔNG TIN VÀ TRUYỀN THÔNG CỤC BÁO CHÍ
Số: /GP-CBC Hà Nội, ngày tháng 01 năm 2023
GIẤY PHÉP MỞ CHUYÊN TRANG CỦA BÁO ĐIỆN TỬ
CỤC TRƯỞNG CỤC BÁO CHÍ
Căn cứ Luật Báo chí ngày 05 tháng 4 năm 2016;
Căn cứ Giấy phép hoạt động báo in và báo điện tử số 605/GP-BTTTT ngày 29 tháng 12 năm 2022 của Bộ Thông tin
và Truyền thông cấp cho Báo Bắc Giang;
QUYẾT ĐỊNH: CẤP GIẤY PHÉP MỞ CHUYÊN TRANG CỦA BÁO ĐIỆN TỬ THEO NHỮNG QUY ĐỊNH SAU:
1. Tên cơ quan báo chí: BÁO BẮC GIANG
"""

GP_1989 = """BỘ THÔNG TIN VÀ TRUYỀN THÔNG CỘNG HOÀ XÃ HỘI CHỦ NGHĨA VIỆT NAM
Số: 2217/GP-BTTTT Hà Nội, ngày 19 tháng 12 năm 2011
GIẤY PHÉP HOẠT ĐỘNG BÁO CHÍ IN
Căn cứ Luật Báo chí ngày 28 tháng 12 năm 1989 và Luật Sửa đổi, bổ sung một số điều của Luật Báo chí
ngày 12 tháng 6 năm 1999; Căn cứ Nghị định 51/2002/NĐ-CP ngày 26 tháng 4 năm 2002 của Chính phủ;
QUYẾT ĐỊNH: CẤP GIẤY PHÉP HOẠT ĐỘNG BÁO CHÍ THEO NHỮNG QUY ĐỊNH SAU:
1. Tên cơ quan chủ quản: Tỉnh uỷ An Giang
"""


def field(
    value: object, confidence: str = "high", note: str | None = None, page: int | None = 1
) -> dict[str, object]:
    return {"value": value, "confidence": confidence, "note": note, "source_page": page}


def sample_extraction(**overrides: object) -> dict[str, object]:
    """Output model hợp lệ (GiayPhepCore) dựa trên GP 635/GP-BTTTT Báo An Giang."""
    data: dict[str, object] = {
        "loai_van_ban": "GP_HOAT_DONG_LUAT_2016",
        "ten_loai_giay_phep": "Giấy phép hoạt động báo in và báo điện tử",
        "so_gp": field("635/GP-BTTTT", "medium", "số viết tay"),
        "ngay_cap": field("29/9/2021"),
        "co_quan_cap": field("Bộ Thông tin và Truyền thông"),
        "nguoi_ky": field("Phạm Anh Tuấn", page=3),
        "chuc_vu_nguoi_ky": field("KT. Bộ trưởng - Thứ trưởng", page=3),
        "can_cu_gp_goc": field(None),
        "van_ban_de_nghi": field("Văn bản số 191-CV/TU của Tỉnh ủy An Giang"),
        "co_quan_chu_quan": {
            "ten": field("Tỉnh ủy An Giang"),
            "dia_chi": field(
                "Số 01, đường Tôn Đức Thắng, phường Mỹ Bình, thành phố Long Xuyên, tỉnh An Giang"
            ),
            "dien_thoai": field("0296.3852223"),
            "fax": field(None),
        },
        "co_quan_bao_chi": {
            "ten": field("Báo An Giang"),
            "loai_hinh_dien_tu": field(None),
            "ten_chuyen_trang": field(None),
        },
        "ton_chi_muc_dich": field("- Tuyên truyền đường lối / - Phản ánh tâm tư", page=2),
        "doi_tuong_phuc_vu": field("Cán bộ, đảng viên, nhân dân trong Tỉnh và bạn đọc quan tâm.", page=2),
        "pham_vi_phat_hanh": field(None),
        "phuong_thuc_phat_hanh": field(None),
        "tru_so": {
            "dia_chi": field(
                "399B Hà Hoàng Hổ, phường Mỹ Xuyên, thành phố Long Xuyên, tỉnh An Giang", page=2
            ),
            "dien_thoai": field("0296.3841854", page=2),
            "fax": field(None),
            "email": field("toasoanbaoangiang@gmail.com", page=2),
            "website": field(None),
        },
        "hieu_luc": field("Có hiệu lực kể từ ngày ký", page=3),
        "gp_duoc_thay_the": [{"so_gp": "2217/GP-BTTTT", "ngay": "19/12/2011"}],
        "an_pham": [
            {
                "loai_hinh": "BAO_IN",
                "cap": "Ấn phẩm chính",
                "ten_goi": "Báo An Giang",
                "ngon_ngu": "Tiếng Việt",
                "ky_han": "05 kỳ/tuần",
                "thoi_gian_phat_hanh": None,
                "khuon_kho": "30cm x 42cm",
                "so_trang": "12 trang",
                "so_luong": None,
                "noi_in": None,
                "ten_mien": None,
                "isp": None,
            },
            {
                "loai_hinh": "BAO_DIEN_TU",
                "cap": None,
                "ten_goi": "Báo An Giang Online",
                "ngon_ngu": "Tiếng Việt",
                "ky_han": None,
                "thoi_gian_phat_hanh": None,
                "khuon_kho": None,
                "so_trang": None,
                "so_luong": None,
                "noi_in": None,
                "ten_mien": "baoangiang.com.vn",
                "isp": "Viettel IDC",
            },
        ],
        "lanh_dao": [{"chuc_vu": "TONG_BIEN_TAP", "ho_ten": "Trần Thị Bích Vân"}],
    }
    data.update(overrides)
    return data
