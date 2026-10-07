"""Lỗi API đối tác: luôn trả `{"error": {"code", "message", "request_id", "details"?}}`.

Danh sách mã lỗi (ERROR_CODES) được dùng cho docs/PARTNER_API.md và test.
"""

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

PREFIX = "/v1"
log = logging.getLogger(__name__)

# code -> (HTTP status, ý nghĩa)
ERROR_CODES: dict[str, tuple[int, str]] = {
    "invalid_request": (422, "Tham số sai/thiếu; xem details"),
    "missing_api_key": (401, "Thiếu header Authorization: Bearer <api_key>"),
    "invalid_api_key": (401, "API key sai hoặc không tồn tại"),
    "api_key_revoked": (401, "API key đã bị thu hồi"),
    "api_key_expired": (401, "API key đã hết hạn"),
    "insufficient_scope": (403, "API key không có quyền (scope) cho thao tác này"),
    "not_found": (404, "Không tìm thấy (hoặc không thuộc tenant của key)"),
    "method_not_allowed": (405, "Sai phương thức HTTP"),
    "not_ready": (409, "Chưa có kết quả để tải (chưa completed)"),
    "not_final": (409, "Document chưa ở trạng thái cuối, chưa thể gửi lại webhook"),
    "idempotency_in_progress": (409, "Request cùng Idempotency-Key đang được xử lý"),
    "idempotency_key_reused": (422, "Idempotency-Key đã dùng cho request có nội dung khác"),
    "file_too_large": (413, "File vượt dung lượng cho phép"),
    "unsupported_media_type": (415, "File không phải PDF / ZIP"),
    "invalid_pdf": (422, "PDF hỏng hoặc có mật khẩu"),
    "too_many_pages": (422, "PDF vượt số trang cho phép"),
    "too_many_files": (422, "Quá nhiều file trong 1 request"),
    "invalid_zip": (422, "ZIP hỏng, rỗng hoặc chứa file không hợp lệ"),
    "invalid_webhook_url": (422, "webhook_url không hợp lệ (phải https, không trỏ vào mạng nội bộ)"),
    "page_quota_exceeded": (402, "Vượt hạn mức số trang/tháng của tenant"),
    "budget_exceeded": (402, "Vượt hạn mức chi phí (VND)/tháng của tenant"),
    "rate_limited": (429, "Vượt số request/phút; thử lại sau Retry-After giây"),
    "service_unavailable": (503, "Hàng đợi tạm thời không sẵn sàng; thử lại"),
    "internal_error": (500, "Lỗi hệ thống"),
}


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        details: Any = None,
    ) -> None:
        self.code = code
        self.status = status or ERROR_CODES[code][0]
        self.message = message or ERROR_CODES[code][1]
        self.headers = headers
        self.details = details
        super().__init__(self.message)


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid is None:
        rid = request.state.request_id = f"req_{uuid.uuid4().hex[:20]}"
    return str(rid)


def error_response(
    request: Request,
    code: str,
    message: str,
    status: int,
    headers: dict[str, str] | None = None,
    details: Any = None,
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id(request)}
    if details is not None:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status, headers=headers)


_HTTP_CODE = {401: "invalid_api_key", 403: "insufficient_scope", 404: "not_found", 405: "method_not_allowed"}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> Response:
        return error_response(request, exc.code, exc.message, exc.status, exc.headers, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        if not request.url.path.startswith(PREFIX):
            return await request_validation_exception_handler(request, exc)
        # Chỉ trả vị trí + thông báo, không lặp lại giá trị đầu vào (có thể chứa dữ liệu đối tác)
        details = [{"loc": [str(x) for x in e["loc"]], "msg": e["msg"]} for e in exc.errors()]
        return error_response(request, "invalid_request", "Tham số không hợp lệ", 422, details=details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        if not request.url.path.startswith(PREFIX):
            return await http_exception_handler(request, exc)
        code = _HTTP_CODE.get(
            exc.status_code, "internal_error" if exc.status_code >= 500 else "invalid_request"
        )
        return error_response(request, code, str(exc.detail), exc.status_code, getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        log.error("lỗi không xử lý", extra={"path": request.url.path}, exc_info=exc)
        if not request.url.path.startswith(PREFIX):
            return PlainTextResponse("Internal Server Error", status_code=500)
        return error_response(request, "internal_error", ERROR_CODES["internal_error"][1], 500)
