"""Prompt bóc tách theo từng mẫu (few-shot 1 ví dụ rút gọn) và prompt phân loại."""

import json

from app.models.schema import LoaiVanBan

SYSTEM_PROMPT = """Bạn là chuyên viên số hóa hồ sơ của Cục Báo chí Việt Nam.
Nhiệm vụ: đọc Giấy phép báo chí (lớp chữ và/hoặc ảnh scan) và điền JSON đúng schema.

QUY TẮC BẮT BUỘC:
1. Giữ nguyên chính tả, viết hoa, dấu câu như văn bản gốc. KHÔNG tự sửa lỗi, KHÔNG diễn giải.
   Ngoại lệ duy nhất: ngày tháng chuẩn hóa về dd/mm/yyyy (vd "ngày 5 tháng 1 năm 2023" -> "05/01/2023").
2. Trường không có trong văn bản -> value = null. KHÔNG được đoán hay suy ra từ kiến thức bên ngoài.
3. Mỗi trường có confidence:
   - "high": chữ in rõ ràng.
   - "medium": chữ viết tay/đóng dấu nhưng đọc được rõ, hoặc phải ghép từ nhiều chỗ trong văn bản.
   - "low": mờ, bị dấu che, viết tay khó đọc -> vẫn điền giá trị đọc được và ghi lý do vào note.
4. source_page = số trang logic chứa thông tin (theo nhãn "Trang N").
5. Số GP thường là chữ viết tay/đóng số trước "/GP-...": ghi đủ dạng "605/GP-BTTTT".
   Nếu chỗ số bị bỏ trống (bản chưa ký) -> value = null, note = "chưa điền số".
6. Ngày cấp lấy ở dòng "Hà Nội, ngày ... tháng ... năm ...". Ô ngày bỏ trống -> null + note.
7. Người ký: họ tên dưới chữ ký. chuc_vu_nguoi_ky ghi gộp dòng trên chữ ký, vd "KT. Bộ trưởng - Thứ trưởng",
   "KT. Cục trưởng - Phó Cục trưởng".
8. Văn bản nhiều dòng (tôn chỉ mục đích...) giữ gạch đầu dòng, nối các dòng bằng " / ".
9. gp_duoc_thay_the: mọi GP cũ được nêu là "thay thế" (số + ngày dd/mm/yyyy). an_pham: mỗi ấn phẩm
   (báo in chính/khác, báo điện tử, chuyên trang) một phần tử. lanh_dao: Tổng biên tập, Phó Tổng biên tập.
10. Không ghi mã vùng/điện thoại theo dạng khác bản gốc (giữ dấu chấm, khoảng trắng như gốc).
"""

_COMMON_FIELDS = """Ánh xạ mục -> trường JSON:
- Tiêu đề in hoa "GIẤY PHÉP ..." -> ten_loai_giay_phep (nguyên văn, viết thường như câu: "Giấy phép ...").
- Góc trên trái (cơ quan ban hành) -> co_quan_cap.
- "Theo (văn bản) đề nghị ..." -> van_ban_de_nghi: bắt đầu từ "Văn bản số ...", bỏ cụm mở đầu
  "Theo đề nghị tại"/"Theo"; giữ nguyên số hiệu, ngày, cơ quan đề nghị.
- can_cu_gp_goc CHỈ dùng cho GP mở chuyên trang (GP hoạt động gốc của báo). Các căn cứ Luật, Nghị định,
  Thông tư KHÔNG phải can_cu_gp_goc. GP hoạt động (1989/2016) -> can_cu_gp_goc = null.
"""

_TEMPLATE_HINTS: dict[LoaiVanBan, str] = {
    LoaiVanBan.GP_HOAT_DONG_LUAT_1989: """Mẫu: Giấy phép hoạt động báo chí (in hoặc điện tử) theo Luật Báo chí 1989, 11-12 mục:
1. Tên cơ quan chủ quản (+ địa chỉ, điện thoại, fax) -> co_quan_chu_quan
2. Tên cơ quan báo chí -> co_quan_bao_chi.ten
3. Tôn chỉ, mục đích -> ton_chi_muc_dich
4. Đối tượng phục vụ -> doi_tuong_phuc_vu
5. Phạm vi phát hành (chủ yếu) -> pham_vi_phat_hanh
6. Thể thức xuất bản (tên gọi, ngôn ngữ, kỳ hạn, khuôn khổ, số trang, số lượng, nơi in; báo điện tử: tên miền)
   -> an_pham (mỗi ấn phẩm chính/khác một phần tử; cap = "Ấn phẩm chính"/"Ấn phẩm khác")
7. Phương thức phát hành -> phuong_thuc_phat_hanh (báo điện tử: "Công ty cung cấp dịch vụ kết nối Internet" -> an_pham.isp)
8. Trụ sở tòa soạn -> tru_so
9. Lãnh đạo cơ quan báo chí -> lanh_dao
10-12. Thời hạn hoạt động / hiệu lực (thường "mười (10) năm kể từ ngày ký") -> hieu_luc (ghi đủ ý)
Mẫu này KHÔNG có can_cu_gp_goc.""",
    LoaiVanBan.GP_HOAT_DONG_LUAT_2016: """Mẫu: Giấy phép hoạt động báo in / báo in và báo điện tử theo Luật Báo chí 2016, ~10 mục:
1. Tên cơ quan chủ quản báo chí (+ địa chỉ, điện thoại, fax) -> co_quan_chu_quan
2. Tên cơ quan báo chí -> co_quan_bao_chi.ten
3. Tôn chỉ, mục đích -> ton_chi_muc_dich
4. Đối tượng phục vụ -> doi_tuong_phuc_vu
5. Các loại hình: 5.1 Báo in (tên gọi, ngôn ngữ, kỳ hạn, thời gian phát hành, khuôn khổ, số trang; ấn phẩm khác)
   -> an_pham loai_hinh=BAO_IN; 5.2 Báo điện tử (tên gọi, ngôn ngữ, tên miền, đơn vị cung cấp kết nối internet)
   -> an_pham loai_hinh=BAO_DIEN_TU (ten_mien, isp)
6. Trụ sở chính -> tru_so (email = "địa chỉ thư điện tử")
7. Lãnh đạo cơ quan báo chí -> lanh_dao
9. Hiệu lực giấy phép -> hieu_luc; GP cũ được "thay thế" -> gp_duoc_thay_the
Mẫu này thường KHÔNG có phạm vi phát hành, phương thức phát hành -> null.""",
    LoaiVanBan.GP_MO_CHUYEN_TRANG: """Mẫu: Giấy phép mở chuyên trang của báo điện tử, số dạng "xx/GP-CBC", do Cục Báo chí cấp:
- "Căn cứ Giấy phép hoạt động ... số .../GP-BTTTT ngày ..." -> can_cu_gp_goc, bỏ chữ "Căn cứ" ở đầu
1. Tên cơ quan báo chí -> co_quan_bao_chi.ten; "Loại hình điện tử" -> co_quan_bao_chi.loai_hinh_dien_tu;
   địa chỉ/điện thoại/fax/thư điện tử của cơ quan báo chí -> tru_so
2. Tên chuyên trang -> co_quan_bao_chi.ten_chuyen_trang (giữ nguyên như ghi; nếu tiêu đề in hoa toàn bộ
   thì lấy cách viết trong phần "Căn cứ/đề nghị" nếu có)
3. Tôn chỉ, mục đích; 4. Đối tượng phục vụ
5. Thể thức (ngôn ngữ, tên miền) -> an_pham: 1 phần tử loai_hinh=CHUYEN_TRANG, ten_goi = tên chuyên trang
- Hiệu lực -> hieu_luc
- Cơ quan chủ quản thường không ghi trực tiếp -> để null (KHÔNG suy ra).
Mẫu này KHÔNG có lanh_dao -> [].""",
}

# Ví dụ rút gọn để mô tả định dạng (không phải dữ liệu thật đầy đủ)
_FEWSHOT: dict[LoaiVanBan, dict[str, object]] = {
    LoaiVanBan.GP_HOAT_DONG_LUAT_1989: {
        "so_gp": {"value": "2217/GP-BTTTT", "confidence": "medium", "note": "số viết tay", "source_page": 1},
        "ngay_cap": {
            "value": "19/12/2011",
            "confidence": "medium",
            "note": "ngày, tháng viết tay",
            "source_page": 1,
        },
        "chuc_vu_nguoi_ky": {
            "value": "KT. Bộ trưởng - Thứ trưởng",
            "confidence": "high",
            "note": None,
            "source_page": 2,
        },
        "an_pham": [
            {
                "loai_hinh": "BAO_IN",
                "cap": "Ấn phẩm chính",
                "ten_goi": "Báo An Giang",
                "ngon_ngu": "Tiếng Việt",
                "ky_han": "05 kỳ/tuần",
                "khuon_kho": "29cm x 42cm",
                "so_trang": "12 trang",
                "so_luong": "10.000 bản/kỳ",
                "noi_in": "Tỉnh An Giang",
            }
        ],
        "lanh_dao": [{"chuc_vu": "TONG_BIEN_TAP", "ho_ten": "Tân Văn Ngữ"}],
    },
    LoaiVanBan.GP_HOAT_DONG_LUAT_2016: {
        "so_gp": {"value": "635/GP-BTTTT", "confidence": "medium", "note": "số viết tay", "source_page": 1},
        "gp_duoc_thay_the": [{"so_gp": "2217/GP-BTTTT", "ngay": "19/12/2011"}],
        "an_pham": [
            {
                "loai_hinh": "BAO_IN",
                "cap": "Ấn phẩm chính",
                "ten_goi": "Báo An Giang",
                "ngon_ngu": "Tiếng Việt",
                "ky_han": "05 kỳ/tuần",
                "thoi_gian_phat_hanh": "Từ thứ Hai đến thứ Sáu hằng tuần",
                "khuon_kho": "30cm x 42cm",
                "so_trang": "12 trang",
            },
            {
                "loai_hinh": "BAO_DIEN_TU",
                "ten_goi": "Báo An Giang Online",
                "ngon_ngu": "Tiếng Việt",
                "ten_mien": "baoangiang.com.vn",
                "isp": "Trung tâm dữ liệu Bình Dương - Viettel IDC Bình Dương",
            },
        ],
    },
    LoaiVanBan.GP_MO_CHUYEN_TRANG: {
        "so_gp": {
            "value": "1/GP-CBC",
            "confidence": "medium",
            "note": "số đóng trong chữ ký số",
            "source_page": 1,
        },
        "can_cu_gp_goc": {
            "value": "Giấy phép hoạt động báo in và báo điện tử số 605/GP-BTTTT ngày 29/12/2022",
            "confidence": "high",
            "note": None,
            "source_page": 1,
        },
        "co_quan_chu_quan": {
            "ten": {"value": None, "confidence": "high", "note": "không ghi trực tiếp", "source_page": None}
        },
        "an_pham": [
            {
                "loai_hinh": "CHUYEN_TRANG",
                "ten_goi": "Bac Giang Online (tiếng Anh)",
                "ngon_ngu": "Tiếng Anh",
                "ten_mien": "en.baobacgiang.vn",
            }
        ],
        "lanh_dao": [],
    },
}


def extraction_user_prompt(loai: LoaiVanBan, *, has_images: bool, has_text: bool) -> str:
    src = []
    if has_text:
        src.append("lớp chữ trích từ PDF (chính xác, ưu tiên dùng)")
    if has_images:
        src.append("ảnh trang (dùng để đọc số/ngày viết tay, đóng dấu, chữ ký số và kiểm tra lại)")
    example = json.dumps(_FEWSHOT.get(loai, {}), ensure_ascii=False, indent=1)
    return (
        f"Nguồn dữ liệu: {'; '.join(src)}.\n"
        f'loai_van_ban = "{loai.value}".\n\n'
        f"{_TEMPLATE_HINTS.get(loai, '')}\n\n{_COMMON_FIELDS}\n"
        f"Ví dụ định dạng một số trường (từ một giấy phép khác, KHÔNG chép giá trị):\n{example}\n\n"
        "Hãy điền toàn bộ JSON theo schema cho văn bản dưới đây."
    )


CLASSIFY_SYSTEM = "Bạn phân loại văn bản hành chính của Bộ TT&TT / Cục Báo chí. Chỉ trả JSON theo schema."

CLASSIFY_PROMPT = """Xác định loại văn bản trong ảnh:
- GP_HOAT_DONG_LUAT_1989: Giấy phép hoạt động báo chí (in hoặc điện tử), căn cứ Luật Báo chí 28/12/1989.
- GP_HOAT_DONG_LUAT_2016: Giấy phép hoạt động báo in / báo điện tử / báo in và báo điện tử, căn cứ Luật Báo chí 05/4/2016.
- GP_MO_CHUYEN_TRANG: Giấy phép mở chuyên trang của báo điện tử (số .../GP-CBC).
- KHAC: mọi văn bản khác (tờ khai, đề án, phiếu trình, công văn, măng sét, giao diện, GP thiết lập trang tin điện tử...).
Chỉ chọn 1 trong 3 loại GP khi có tiêu đề "GIẤY PHÉP ..." VÀ phần "QUYẾT ĐỊNH". ten_loai_giay_phep = tiêu đề nguyên văn
(null nếu KHAC). ly_do: 1 câu ngắn."""
