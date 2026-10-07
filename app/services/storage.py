"""Lưu file PDF trên volume DATA_DIR và đọc ZIP thư mục báo."""

import io
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_ZIP_MEMBERS = 500


class UploadError(Exception):
    pass


@dataclass
class IncomingPdf:
    folder_name: str | None
    file_name: str
    data: bytes


def is_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(b"%PDF")


def save_pdf(root: Path, tenant_id: uuid.UUID, doc_id: uuid.UUID, data: bytes) -> Path:
    path = root / str(tenant_id) / f"{doc_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _member_name(info: zipfile.ZipInfo) -> str:
    """ZIP tạo trên Windows thường không bật cờ UTF-8 -> Python giải mã cp437, tên tiếng Việt bị lỗi."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def read_zip(data: bytes, *, max_file_bytes: int) -> list[IncomingPdf]:
    """Mỗi PDF trong ZIP -> 1 file; `folder_name` = thư mục chứa PDF (đường dẫn tương đối trong ZIP)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UploadError("File ZIP hỏng hoặc không phải ZIP") from e
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ZIP_MEMBERS:
        raise UploadError(f"ZIP có {len(infos)} file, vượt giới hạn {MAX_ZIP_MEMBERS}")
    out: list[IncomingPdf] = []
    for info in infos:
        path = PurePosixPath(_member_name(info).replace("\\", "/"))
        if any(part.startswith((".", "__MACOSX")) for part in path.parts):
            continue
        if path.suffix.lower() != ".pdf":
            continue
        if info.file_size > max_file_bytes:
            raise UploadError(f"{path}: vượt giới hạn {max_file_bytes // (1024 * 1024)}MB")
        content = zf.read(info)
        if not is_pdf(content):
            raise UploadError(f"{path}: không phải PDF")
        folder = str(path.parent) if str(path.parent) != "." else None
        out.append(IncomingPdf(folder, path.name, content))
    if not out:
        raise UploadError("ZIP không chứa file PDF nào")
    return out
