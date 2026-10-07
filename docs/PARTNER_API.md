# VTCDocSense – Tài liệu API cho đối tác (v1)

API nhận file PDF **Giấy phép hoạt động báo chí**, trả về các trường đã bóc tách dưới dạng JSON (kèm độ tin cậy từng trường) và file Excel.

| | |
|---|---|
| Base URL | `https://apidocsense.vtcdigital.top` |
| Tiền tố | `/v1` |
| Tài liệu tương tác (OpenAPI) | `https://apidocsense.vtcdigital.top/v1/docs` · spec: `/v1/openapi.json` |
| Postman collection | [`docs/postman/VTCDocSense_Partner_API.postman_collection.json`](postman/VTCDocSense_Partner_API.postman_collection.json) |
| Định dạng | Upload `multipart/form-data`, response JSON UTF-8, thời gian ISO 8601 (UTC) |

---

## Mục lục

1. [Môi trường: sandbox và live](#1-môi-trường-sandbox-và-live)
2. [Xác thực](#2-xác-thực)
3. [Quy trình tích hợp](#3-quy-trình-tích-hợp)
4. [Trạng thái document](#4-trạng-thái-document)
5. [Danh sách endpoint](#5-danh-sách-endpoint)
6. [Cấu trúc kết quả](#6-cấu-trúc-kết-quả)
7. [Webhook](#7-webhook)
8. [Idempotency và file trùng](#8-idempotency-và-file-trùng)
9. [Giới hạn](#9-giới-hạn)
10. [Mã lỗi](#10-mã-lỗi)
11. [Ví dụ đầy đủ: curl, Python, JavaScript](#11-ví-dụ-đầy-đủ)
12. [Bảo mật và lưu trữ dữ liệu](#12-bảo-mật-và-lưu-trữ-dữ-liệu)

---

## 1. Môi trường: sandbox và live

VTCDocSense cấp cho mỗi đối tác (tenant) hai loại API key:

| Key | Chế độ | Xử lý | Chi phí |
|---|---|---|---|
| `ds_test_…` | **Sandbox** | Không chạy AI. Luôn trả **kết quả mẫu cố định** (GP mẫu của "Báo An Giang") | Miễn phí, không tính hạn mức |
| `ds_live_…` | **Live** | Bóc tách thật từ nội dung PDF | Tính theo số trang/chi phí trong hạn mức |

- Sandbox chạy **đúng vòng đời** của bản thật: 202 → `queued` → `processing` → trạng thái cuối → webhook → tải kết quả. Bạn dùng sandbox để viết và kiểm thử toàn bộ phần tích hợp, sau đó chỉ cần đổi key sang `ds_live_`.
- Dữ liệu của hai chế độ **tách biệt hoàn toàn**: key sandbox không thấy document live và ngược lại.
- Sandbox vẫn kiểm tra file như bản thật (đúng PDF, dung lượng, số trang), nên lỗi upload của bạn sẽ lộ ra ngay trong sandbox.
- Kết quả sandbox có `"sandbox": true`. **Không dùng kết quả sandbox làm dữ liệu thật.**

**Mô phỏng tình huống (chỉ sandbox):** gửi thêm trường `sandbox_scenario` khi upload:

| `sandbox_scenario` | Kết quả |
|---|---|
| *(bỏ trống)* hoặc `completed` | `completed` với kết quả mẫu |
| `failed` | `failed`, `error.code = processing_failed` |
| `rejected` | `rejected` (người duyệt từ chối) |

---

## 2. Xác thực

Gửi API key trong mọi request:

```
Authorization: Bearer ds_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

- Key do VTCDocSense cấp và **chỉ hiển thị một lần** khi tạo. Hệ thống chỉ lưu bản băm (hash) của key, nên nếu bạn làm mất key thì phải xin cấp key mới.
- Mỗi key thuộc một tenant, có **scope** (quyền) và có thể có **ngày hết hạn**:

| Scope | Cho phép |
|---|---|
| `documents:write` | Upload (`POST /v1/documents`, `POST /v1/batches`), gửi lại webhook |
| `documents:read` | Xem trạng thái/kết quả, tải Excel/ZIP, `GET /v1/usage` |

- Key hết hạn hoặc bị thu hồi sẽ nhận `401` với mã lỗi `api_key_expired` / `api_key_revoked`.
- Không để key trong mã nguồn phía trình duyệt/app di động. Chỉ gọi API từ **máy chủ** của bạn.

---

## 3. Quy trình tích hợp

```
Hệ thống đối tác                                VTCDocSense
      │  POST /v1/documents (PDF, external_id,       │
      │        webhook_url, Idempotency-Key)         │
      │ ───────────────────────────────────────────► │
      │  202 {document.id, status: "queued"}         │
      │ ◄─────────────────────────────────────────── │
      │                                    xử lý (+ người duyệt nếu tenant yêu cầu)
      │  POST webhook_url  document.completed        │
      │  (X-DocSense-Signature)                      │
      │ ◄─────────────────────────────────────────── │
      │  2xx                                         │
      │ ───────────────────────────────────────────► │
      │  GET /v1/documents/{id}  → JSON kết quả      │
      │  GET /v1/documents/{id}/export.xlsx          │
      │ ───────────────────────────────────────────► │
```

1. **Nhận key sandbox** (`ds_test_…`) và **webhook secret** (`whsec_…`) từ VTCDocSense.
2. **Upload** PDF bằng `POST /v1/documents` (hoặc ZIP bằng `POST /v1/batches`). Mỗi lần upload nên kèm:
   - `external_id`: mã hồ sơ bên bạn, để đối chiếu khi nhận webhook;
   - `webhook_url`: URL HTTPS bên bạn để nhận kết quả;
   - header `Idempotency-Key`: một UUID mới cho mỗi lần upload, để retry an toàn.
3. **Lưu `document.id`** từ response 202.
4. **Nhận webhook** khi document tới trạng thái cuối. Xác minh chữ ký (mục 7), trả `2xx` trong vòng 10 giây, rồi xử lý bất đồng bộ.
5. **Lấy kết quả** bằng `GET /v1/documents/{id}` (JSON) và/hoặc `GET /v1/documents/{id}/export.xlsx`.
6. Nếu không dùng được webhook, bạn có thể **poll** `GET /v1/documents/{id}`, khoảng 5–10 giây/lần và tôn trọng rate limit.
7. Theo dõi hạn mức bằng `GET /v1/usage`.
8. Khi chạy ổn trên sandbox, **đổi sang key `ds_live_`**. Không cần sửa gì khác.

---

## 4. Trạng thái document

| `status` | Ý nghĩa | Là trạng thái cuối? |
|---|---|---|
| `queued` | Đã nhận, chờ xử lý | |
| `processing` | Đang xử lý | |
| `pending_review` | Đã bóc tách xong, **đang chờ người của VTCDocSense duyệt** (chưa có kết quả) | |
| `completed` | Có kết quả | ✔ |
| `rejected` | Người duyệt từ chối (vd file không phải giấy phép báo chí) | ✔ |
| `failed` | Không xử lý được, xem `error.code` | ✔ |

**Cấu hình theo tenant `require_human_review`** (VTCDocSense thiết lập theo hợp đồng):

- `true` (mặc định): bạn **chỉ nhận kết quả sau khi người của VTCDocSense đã duyệt**. Trước đó, trạng thái là `pending_review` và `result = null`. Khi `completed`, `needs_review = false`.
- `false`: trả kết quả **ngay khi máy xử lý xong** (`completed`), kèm:
  - `needs_review`: `true` nếu hệ thống khuyên kiểm tra lại (có trường trọng yếu độ tin cậy chưa cao);
  - `confidence` của từng trường (`high` | `medium` | `low`).

**Cập nhật sau trạng thái cuối:** nếu kết quả đã gửi được chỉnh sửa (vd người duyệt sửa một trường), bạn nhận webhook `document.updated` và `result_version` tăng lên.

---

## 5. Danh sách endpoint

| Method | Path | Scope | Mô tả |
|---|---|---|---|
| `POST` | `/v1/documents` | write | Upload 1..N PDF → 202 |
| `POST` | `/v1/batches` | write | Upload 1 ZIP (thư mục hồ sơ) → 202 |
| `GET` | `/v1/documents` | read | Danh sách (lọc `external_id`, `batch_id`, `status`, `limit`) |
| `GET` | `/v1/documents/{id}` | read | Trạng thái + kết quả JSON |
| `GET` | `/v1/documents/{id}/export.xlsx` | read | Kết quả Excel (chỉ khi `completed`) |
| `GET` | `/v1/documents/{id}/webhooks` | read | Lịch sử gửi webhook của document |
| `POST` | `/v1/documents/{id}/webhook/resend` | write | Gửi lại webhook trạng thái cuối |
| `GET` | `/v1/batches/{id}` | read | Trạng thái batch + danh sách document |
| `GET` | `/v1/batches/{id}/export.zip` | read | ZIP: `<thư mục>/<tên>.xlsx` + `TongHop.xlsx` (các document `completed`) |
| `GET` | `/v1/schema` | bất kỳ | JSON Schema của `result` + danh sách trạng thái |
| `GET` | `/v1/usage` | read | Số trang, chi phí, số lần gọi AI trong kỳ + hạn mức |

### `POST /v1/documents`

`multipart/form-data`:

| Trường | Bắt buộc | Mô tả |
|---|---|---|
| `files` | ✔ | 1..20 file PDF (lặp trường `files`) |
| `external_id` | | Mã của bạn (≤ 255 ký tự). Nhiều file thì lặp lại **đúng thứ tự file**, mỗi file một giá trị |
| `batch_id` | | Thêm vào batch đã có (của chính bạn). Bỏ trống thì tạo batch mới |
| `webhook_url` | | URL `https://` nhận sự kiện |
| `folder_name` | | Nhóm hồ sơ (vd tên cơ quan báo chí); các GP cùng nhóm được đối chiếu chéo số/ngày |
| `sandbox_scenario` | | Chỉ dùng với key `ds_test_`, xem mục 1 |

Header tùy chọn: `Idempotency-Key`.

Response `202`:

```json
{
  "object": "upload",
  "batch_id": "6c1e…",
  "documents": [
    {
      "id": "5afc188d-bc04-4f01-a364-d36faecb157e",
      "object": "document",
      "external_id": "HS-2026-0001",
      "batch_id": "6c1e…",
      "file_name": "GiayPhep.pdf",
      "folder_name": null,
      "sha256": "9f2c…",
      "status": "queued",
      "needs_review": null,
      "document_type": null,
      "pages": 2,
      "pdf_type": "SCAN",
      "sandbox": false,
      "deduplicated": false,
      "created_at": "2026-10-07T03:15:22.120000+00:00",
      "updated_at": "2026-10-07T03:15:22.120000+00:00",
      "error": null,
      "result_version": null,
      "result": null
    }
  ]
}
```

### `POST /v1/batches`

`multipart/form-data`: `file` (ZIP, bắt buộc), `name`, `webhook_url`, `sandbox_scenario`. Mỗi PDF trong ZIP thành một document; `folder_name` là thư mục chứa PDF trong ZIP. Tên file tiếng Việt trong ZIP tạo trên Windows được hỗ trợ. Các file ẩn và `__MACOSX` bị bỏ qua. Nếu có `webhook_url`, bạn nhận sự kiện của **từng document**, cộng thêm một sự kiện `batch.completed` khi cả batch xong.

### `GET /v1/documents/{id}`

Trả object document như trên. Khi `status = completed`, object có thêm `result` (mục 6) và `result_version`. Khi `failed`, xem `error`:

```json
"error": { "code": "invalid_pdf", "message": "Không đọc được PDF (hỏng, có mật khẩu hoặc vượt giới hạn)" }
```

| `error.code` (document) | Ý nghĩa |
|---|---|
| `invalid_pdf` | PDF không đọc được khi xử lý |
| `processing_failed` | Lỗi xử lý; liên hệ VTCDocSense kèm `id` |

### `GET /v1/usage?period=YYYY-MM`

Kỳ tính theo tháng (giờ Việt Nam), mặc định là tháng hiện tại. Số liệu tính riêng cho chế độ của key (live/sandbox).

```json
{
  "object": "usage",
  "period": "2026-10",
  "mode": "live",
  "documents": 120,
  "pages": 342,
  "cost_vnd": 51230.5,
  "llm_calls": 245,
  "limits": {
    "monthly_page_quota": 5000,
    "pages_remaining": 4658,
    "monthly_budget_vnd": 2000000,
    "budget_remaining_vnd": 1948769.5,
    "rate_limit_per_minute": 60
  }
}
```

`pages`/`documents` chỉ đếm file được xử lý thật. File trùng (mục 8) không bị tính.

---

## 6. Cấu trúc kết quả

JSON Schema đầy đủ: `GET /v1/schema` (`schema_version = "v1"`). Mỗi trường đơn có dạng:

```json
"so_gp": {
  "value": "635/GP-BTTTT",
  "confidence": "medium",
  "note": "số viết tay",
  "source_page": 1,
  "verified_by": null
}
```

| Khóa | Ý nghĩa |
|---|---|
| `value` | Giá trị (`null` nếu văn bản không có). `ngay_cap` dạng `YYYY-MM-DD` |
| `confidence` | `high` / `medium` / `low` |
| `note` | Ghi chú kiểm tra (vd "số viết tay") |
| `source_page` | Trang (logic) chứa thông tin |
| `verified_by` | `null`; `"chu_ky_so"` (khớp chữ ký số); `"ra_soat"` (người duyệt đã xác nhận/sửa); hoặc nguồn đối chiếu chéo |

Các nhóm trường chính: `loai_van_ban` (`GP_HOAT_DONG_LUAT_1989` | `GP_HOAT_DONG_LUAT_2016` | `GP_MO_CHUYEN_TRANG` | `KHAC`), `so_gp`, `ngay_cap`, `co_quan_cap`, `nguoi_ky`, `co_quan_chu_quan{ten, dia_chi, dien_thoai, fax}`, `co_quan_bao_chi{ten, …}`, `ton_chi_muc_dich`, `doi_tuong_phuc_vu`, `tru_so{…}`, `hieu_luc`, `gp_duoc_thay_the[]`, `an_pham[]`, `lanh_dao[]`, `needs_review`, `review_reasons[]`, `meta{file_name, pages, logical_pages, pdf_type}`.

Bốn **trường trọng yếu** quyết định `needs_review` là `so_gp`, `ngay_cap`, `co_quan_bao_chi.ten` và `co_quan_chu_quan.ten`.

---

## 7. Webhook

### Sự kiện

| `type` | Khi nào |
|---|---|
| `document.completed` | Document có kết quả |
| `document.failed` | Xử lý thất bại |
| `document.rejected` | Người duyệt từ chối |
| `document.updated` | Kết quả đã gửi bị chỉnh sửa (`result_version` tăng) |
| `batch.completed` | Mọi document của batch ZIP đã ở trạng thái cuối (chỉ khi upload ZIP có `webhook_url`) |

### Request VTCDocSense gửi tới bạn

```
POST {webhook_url}
Content-Type: application/json
User-Agent: VTCDocSense-Webhook/0.1.0
X-DocSense-Event: document.completed
X-DocSense-Delivery: 0b6f…              (id lần gửi)
X-DocSense-Signature: t=1791342922,v1=5d41402abc4b2a76b9719d911017c592…
```

```json
{
  "id": "evt_3f0c9a…",
  "object": "event",
  "type": "document.completed",
  "created_at": "2026-10-07T03:15:30.512000+00:00",
  "data": {
    "document": { "id": "5afc…", "external_id": "HS-2026-0001", "status": "completed", "needs_review": false, "result_version": 1, "…": "…" }
  }
}
```

Webhook **không chứa `result`**. Gọi `GET /v1/documents/{id}` để lấy kết quả, vì như vậy dữ liệu không đi qua kênh không xác thực, và bạn luôn nhận bản mới nhất.

### Xác minh chữ ký (bắt buộc)

`X-DocSense-Signature: t=<unix timestamp>,v1=<hex>`, trong đó:

```
v1 = HEX( HMAC_SHA256( key = webhook_secret, message = "<t>." + <raw body bytes> ) )
```

- Dùng **raw body đúng như nhận được**. Không parse JSON rồi serialize lại trước khi tính.
- So sánh bằng hàm **constant-time**.
- **Từ chối nếu `t` lệch quá 5 phút** so với giờ hiện tại (chống gửi lại).
- Dùng `id` của sự kiện (`evt_…`) để **bỏ trùng**: cùng một sự kiện có thể tới nhiều hơn một lần.
- Webhook secret (`whsec_…`) do VTCDocSense cấp riêng cho mỗi tenant. Khi bị xoay (rotate), secret cũ mất hiệu lực ngay.

**Python (Flask):**

```python
import hashlib, hmac, time
from flask import Flask, request, abort

WEBHOOK_SECRET = "whsec_..."
app = Flask(__name__)


def verify(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


@app.post("/docsense/webhook")
def docsense_webhook():
    body = request.get_data()  # raw bytes
    if not verify(WEBHOOK_SECRET, body, request.headers.get("X-DocSense-Signature", "")):
        abort(400)
    event = request.get_json()
    # TODO: bỏ trùng theo event["id"], đưa vào hàng đợi xử lý, rồi trả 2xx ngay
    return "", 204
```

**JavaScript (Node.js + Express):**

```js
const crypto = require("crypto");
const express = require("express");
const WEBHOOK_SECRET = "whsec_...";
const app = express();

function verify(secret, rawBody, header, toleranceS = 300) {
  const parts = Object.fromEntries((header || "").split(",").map((p) => p.split("=", 2)));
  const ts = Number(parts.t);
  if (!ts || !parts.v1 || Math.abs(Date.now() / 1000 - ts) > toleranceS) return false;
  const expected = crypto.createHmac("sha256", secret)
    .update(Buffer.concat([Buffer.from(`${ts}.`), rawBody])).digest("hex");
  const a = Buffer.from(expected), b = Buffer.from(parts.v1);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// express.raw: giữ nguyên body dạng Buffer để tính chữ ký
app.post("/docsense/webhook", express.raw({ type: "application/json" }), (req, res) => {
  if (!verify(WEBHOOK_SECRET, req.body, req.get("X-DocSense-Signature"))) return res.sendStatus(400);
  const event = JSON.parse(req.body.toString("utf8"));
  // TODO: bỏ trùng theo event.id, xử lý bất đồng bộ
  res.sendStatus(204);
});
app.listen(8080);
```

### Retry và gửi lại

- Gửi thành công là khi bạn trả **HTTP 2xx trong vòng 10 giây**. Redirect (3xx) bị coi là thất bại.
- Nếu không thành công, hệ thống thử lại sau **30 giây, 2 phút, 10 phút, 30 phút, 2 giờ, 6 giờ, 12 giờ** (tổng cộng 8 lần trong khoảng 21 giờ). Sau đó lần gửi được đánh dấu `failed`.
- Xem lịch sử gửi: `GET /v1/documents/{id}/webhooks`.
- Gửi lại thủ công (vd sau khi bạn sửa lỗi endpoint): `POST /v1/documents/{id}/webhook/resend`. Body JSON tùy chọn `{"webhook_url": "https://…"}` dùng để đổi sang URL mới. Endpoint chỉ dùng được khi document đã ở trạng thái cuối, nếu chưa sẽ trả `409 not_final`.
- `webhook_url` phải là `https://` và trỏ tới địa chỉ công khai trên Internet. Các địa chỉ nội bộ (localhost, 10.x, 192.168.x, 169.254.x…) bị từ chối.

---

## 8. Idempotency và file trùng

**`Idempotency-Key`**: header tùy chọn trên `POST /v1/documents` và `POST /v1/batches`. Nên dùng một UUID mới cho mỗi lần upload.

- Gửi lại **cùng key và cùng nội dung** (cùng file, cùng tham số) trong vòng 24 giờ: bạn nhận lại **đúng response lần đầu**, có header `Idempotent-Replayed: true`, và hệ thống không tạo document mới. Nhờ vậy bạn có thể retry an toàn khi mất kết nối/timeout.
- Cùng key nhưng nội dung khác: `422 idempotency_key_reused`.
- Lần đầu còn đang xử lý: `409 idempotency_in_progress`. Chờ một chút rồi gửi lại.
- Request bị lỗi (4xx/5xx) **không được lưu**: bạn sửa lỗi rồi gửi lại với cùng key.

**File trùng**: nếu bạn gửi lại **cùng một file** (cùng SHA-256) đã gửi trước đó (cùng tenant, cùng chế độ), hệ thống **không xử lý lại và không tính phí**. Document mới vẫn được tạo, có `id` / `external_id` / `webhook_url` riêng, với `"deduplicated": true`, và nhận lại kết quả của lần trước (cả các chỉnh sửa của người duyệt sau đó). Nếu lần trước bị `failed`, file sẽ được xử lý lại bình thường.

---

## 9. Giới hạn

| Giới hạn | Mặc định |
|---|---|
| Dung lượng 1 file PDF | 20 MB |
| Số trang 1 PDF | 30 trang |
| Số file / request `POST /v1/documents` | 20 |
| Dung lượng 1 request (ZIP hoặc tổng các PDF) | 100 MB |
| Số file PDF trong 1 ZIP | 500 |
| Request / phút / API key | 60 (có thể khác theo hợp đồng) |
| Hạn mức tháng | Số trang/tháng và/hoặc VND/tháng theo hợp đồng |

Mọi response `/v1` đều có `X-Request-ID`. Response của các request đã xác thực có thêm header rate limit:

```
X-Request-ID: req_…            (gửi kèm khi cần hỗ trợ)
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 57
X-RateLimit-Reset: 23          (số giây tới cửa sổ mới)
```

Khi vượt rate limit, bạn nhận `429 rate_limited` kèm header `Retry-After`. Hãy chờ đúng số giây đó rồi gửi lại, nên dùng backoff tăng dần. Khi vượt hạn mức tháng, bạn nhận `402 page_quota_exceeded` / `budget_exceeded`. Hệ thống kiểm tra hạn mức **trước khi nhận file**, nên lỗi này không phát sinh chi phí.

---

## 10. Mã lỗi

Mọi lỗi đều có dạng:

```json
{
  "error": {
    "code": "too_many_pages",
    "message": "GiayPhep.pdf: 34 trang, tối đa 30",
    "request_id": "req_8c1f0a2b3d4e5f6a7b8c",
    "details": null
  }
}
```

Code xử lý lỗi của bạn nên dựa vào `code`, không dựa vào `message`, vì nội dung `message` có thể thay đổi.

| HTTP | `code` | Ý nghĩa / cách xử lý |
|---|---|---|
| 401 | `missing_api_key` | Thiếu header `Authorization: Bearer <api_key>` |
| 401 | `invalid_api_key` | Key sai hoặc không tồn tại |
| 401 | `api_key_revoked` | Key đã bị thu hồi; xin key mới |
| 401 | `api_key_expired` | Key đã hết hạn; xin key mới |
| 402 | `page_quota_exceeded` | Vượt hạn mức trang/tháng |
| 402 | `budget_exceeded` | Vượt hạn mức chi phí (VND)/tháng |
| 403 | `insufficient_scope` | Key không có scope cho thao tác này |
| 404 | `not_found` | Không tìm thấy, **hoặc tài nguyên không thuộc tenant/chế độ của key** |
| 405 | `method_not_allowed` | Sai phương thức HTTP |
| 409 | `not_ready` | Chưa có kết quả để tải (chưa `completed`) |
| 409 | `not_final` | Chưa ở trạng thái cuối, chưa gửi lại webhook được |
| 409 | `idempotency_in_progress` | Request cùng `Idempotency-Key` đang xử lý; thử lại sau |
| 413 | `file_too_large` | File/request vượt dung lượng |
| 415 | `unsupported_media_type` | File không phải PDF |
| 422 | `invalid_request` | Tham số sai/thiếu; xem `details` |
| 422 | `idempotency_key_reused` | `Idempotency-Key` đã dùng cho nội dung khác |
| 422 | `invalid_pdf` | PDF hỏng hoặc có mật khẩu |
| 422 | `too_many_pages` | PDF vượt số trang |
| 422 | `too_many_files` | Quá nhiều file trong một request |
| 422 | `invalid_zip` | ZIP hỏng, rỗng hoặc chứa file không hợp lệ |
| 422 | `invalid_webhook_url` | `webhook_url` không hợp lệ (phải https, không trỏ vào mạng nội bộ) |
| 429 | `rate_limited` | Vượt request/phút; chờ `Retry-After` giây |
| 500 | `internal_error` | Lỗi hệ thống; gửi `request_id` cho VTCDocSense |
| 503 | `service_unavailable` | Hàng đợi tạm thời không sẵn sàng; thử lại (dùng cùng `Idempotency-Key`) |

Lỗi nào nên retry: `429`, `500`, `503` và lỗi mạng/timeout. Khi retry, luôn dùng cùng `Idempotency-Key`. Các lỗi 4xx còn lại cần sửa request trước khi gửi lại.

---

## 11. Ví dụ đầy đủ

Đặt biến môi trường:

```bash
export DOCSENSE_URL=https://apidocsense.vtcdigital.top
export DOCSENSE_KEY=ds_test_xxxxxxxxxxxxxxxxxxxxxxxx      # sandbox khi tích hợp
```

### curl

```bash
# 1) Upload 1 file
curl -sS -X POST "$DOCSENSE_URL/v1/documents" \
  -H "Authorization: Bearer $DOCSENSE_KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F "files=@GiayPhep.pdf;type=application/pdf" \
  -F "external_id=HS-2026-0001" \
  -F "webhook_url=https://partner.example.vn/docsense/webhook"

# Upload nhiều file: lặp files và external_id theo đúng thứ tự
curl -sS -X POST "$DOCSENSE_URL/v1/documents" -H "Authorization: Bearer $DOCSENSE_KEY" \
  -F "files=@a.pdf" -F "external_id=HS-1" -F "files=@b.pdf" -F "external_id=HS-2"

# Upload ZIP thư mục hồ sơ
curl -sS -X POST "$DOCSENSE_URL/v1/batches" -H "Authorization: Bearer $DOCSENSE_KEY" \
  -F "file=@ho_so.zip;type=application/zip" -F "webhook_url=https://partner.example.vn/docsense/webhook"

# 2) Trạng thái + kết quả
curl -sS "$DOCSENSE_URL/v1/documents/<document_id>" -H "Authorization: Bearer $DOCSENSE_KEY"

# 3) Excel / ZIP
curl -sS -o ketqua.xlsx "$DOCSENSE_URL/v1/documents/<document_id>/export.xlsx" -H "Authorization: Bearer $DOCSENSE_KEY"
curl -sS -o batch.zip   "$DOCSENSE_URL/v1/batches/<batch_id>/export.zip"       -H "Authorization: Bearer $DOCSENSE_KEY"

# 4) Gửi lại webhook / usage
curl -sS -X POST "$DOCSENSE_URL/v1/documents/<document_id>/webhook/resend" -H "Authorization: Bearer $DOCSENSE_KEY"
curl -sS "$DOCSENSE_URL/v1/usage?period=2026-10" -H "Authorization: Bearer $DOCSENSE_KEY"
```

### Python (`httpx`)

```python
import os, time, uuid
import httpx

BASE = os.environ["DOCSENSE_URL"]
client = httpx.Client(
    base_url=BASE, headers={"Authorization": f"Bearer {os.environ['DOCSENSE_KEY']}"}, timeout=60
)


def request_with_retry(method: str, url: str, **kw) -> httpx.Response:
    """Retry 429/5xx/lỗi mạng; dùng lại cùng Idempotency-Key (đặt trong kw['headers'])."""
    for attempt in range(6):
        try:
            r = client.request(method, url, **kw)
        except httpx.TransportError:
            time.sleep(2**attempt)
            continue
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")))
            continue
        if r.status_code >= 500:
            time.sleep(2**attempt)
            continue
        return r
    r.raise_for_status()
    return r


def upload(path: str, external_id: str, webhook_url: str | None = None) -> dict:
    data = {"external_id": external_id}
    if webhook_url:
        data["webhook_url"] = webhook_url
    with open(path, "rb") as f:
        content = f.read()
    r = request_with_retry(
        "POST",
        "/v1/documents",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        files={"files": (os.path.basename(path), content, "application/pdf")},
        data=data,
    )
    if r.status_code != 202:
        err = r.json()["error"]
        raise RuntimeError(f"{err['code']}: {err['message']} ({err['request_id']})")
    return r.json()["documents"][0]


def wait_result(doc_id: str, timeout_s: int = 600) -> dict:
    """Chỉ dùng khi không có webhook."""
    end = time.time() + timeout_s
    while time.time() < end:
        d = request_with_retry("GET", f"/v1/documents/{doc_id}").json()
        if d["status"] in ("completed", "failed", "rejected"):
            return d
        time.sleep(5)
    raise TimeoutError(doc_id)


doc = upload("GiayPhep.pdf", "HS-2026-0001")
d = wait_result(doc["id"])
if d["status"] == "completed":
    r = d["result"]
    print(
        r["so_gp"]["value"],
        r["ngay_cap"]["value"],
        r["co_quan_bao_chi"]["ten"]["value"],
        "cần kiểm tra" if d["needs_review"] else "",
    )
    open("ketqua.xlsx", "wb").write(client.get(f"/v1/documents/{doc['id']}/export.xlsx").content)
else:
    print("Không có kết quả:", d["status"], d["error"])
```

### JavaScript (Node.js ≥ 18, `fetch` có sẵn)

```js
import { readFile, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";

const BASE = process.env.DOCSENSE_URL;
const auth = { Authorization: `Bearer ${process.env.DOCSENSE_KEY}` };

async function upload(path, externalId, webhookUrl) {
  const form = new FormData();
  form.append("files", new Blob([await readFile(path)], { type: "application/pdf" }), path.split("/").pop());
  form.append("external_id", externalId);
  if (webhookUrl) form.append("webhook_url", webhookUrl);
  const res = await fetch(`${BASE}/v1/documents`, {
    method: "POST",
    headers: { ...auth, "Idempotency-Key": randomUUID() },
    body: form,
  });
  const body = await res.json();
  if (res.status !== 202) throw new Error(`${body.error.code}: ${body.error.message} (${body.error.request_id})`);
  return body.documents[0];
}

async function getDocument(id) {
  const res = await fetch(`${BASE}/v1/documents/${id}`, { headers: auth });
  if (res.status === 429) {
    await new Promise((r) => setTimeout(r, Number(res.headers.get("Retry-After") || 5) * 1000));
    return getDocument(id);
  }
  return res.json();
}

const doc = await upload("GiayPhep.pdf", "HS-2026-0001");
let d;
do {
  await new Promise((r) => setTimeout(r, 5000));
  d = await getDocument(doc.id);
} while (!["completed", "failed", "rejected"].includes(d.status));

if (d.status === "completed") {
  console.log(d.result.so_gp.value, d.result.ngay_cap.value, d.needs_review);
  const xlsx = await fetch(`${BASE}/v1/documents/${doc.id}/export.xlsx`, { headers: auth });
  await writeFile("ketqua.xlsx", Buffer.from(await xlsx.arrayBuffer()));
}
```

### Postman

Import [`docs/postman/VTCDocSense_Partner_API.postman_collection.json`](postman/VTCDocSense_Partner_API.postman_collection.json), đặt biến collection `base_url`, `api_key` (`ds_test_…`). Request "Upload PDF" tự sinh `Idempotency-Key` và tự lưu `document_id`/`batch_id` cho các request sau.

---

## 12. Bảo mật và lưu trữ dữ liệu

- **Cách ly dữ liệu:** mỗi key chỉ truy cập được dữ liệu của tenant và chế độ (live/sandbox) của key đó. Truy cập tài nguyên của tenant khác luôn nhận `404`, kể cả khi `id` có thật.
- **Key:** chỉ lưu bản băm (hash). Ngay cả VTCDocSense cũng không xem lại được key của bạn.
- **Nhật ký (log):** hệ thống không ghi nội dung PDF hay nội dung trích xuất vào log. Log chỉ chứa metadata (đường dẫn, mã trạng thái, prefix key, `request_id`).
- **Truyền tải:** chỉ qua HTTPS. Webhook được ký HMAC-SHA256 và chỉ gửi tới địa chỉ HTTPS công khai.
- **Lưu trữ:** file PDF gốc và ảnh trang **tự động bị xóa sau `RETENTION_DAYS` ngày** (mặc định 30) kể từ khi upload. Kết quả trích xuất (JSON) vẫn được giữ để bạn tải lại. Idempotency-Key hết hạn sau 24 giờ.
- **Người duyệt:** với `require_human_review = true`, chỉ nhân sự được ủy quyền của VTCDocSense xem tài liệu để duyệt. Email người duyệt không bao giờ xuất hiện trong dữ liệu trả cho bạn (`verified_by = "ra_soat"`).

Hỗ trợ: gửi `request_id` (header `X-Request-ID`) và `document.id` khi liên hệ VTCDocSense.
