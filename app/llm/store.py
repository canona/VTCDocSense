"""Khóa cache, cache phản hồi trên đĩa, sổ chi phí theo ngày, fixture record/replay."""

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.models.schema import SCHEMA_VERSION
from app.pipeline.prompts import PROMPT_VERSION
from app.providers.base import ExtractionRequest, ExtractionResult


def request_digest(req: ExtractionRequest) -> str:
    """sha256 nội dung request (prompt + schema + trang), không gồm model.

    Ảnh có `image_id` thì dùng id (ổn định giữa các máy) thay cho bytes JPEG.
    """
    h = hashlib.sha256()
    head = {
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "schema_name": req.schema_name,
        "system": req.system_prompt,
        "user": req.user_prompt,
        "schema": req.json_schema,
    }
    h.update(json.dumps(head, ensure_ascii=False, sort_keys=True).encode())
    for p in req.pages:
        h.update(f"\x00page{p.page_no}\x00".encode())
        if p.text:
            h.update(p.text.encode())
        if p.image is not None:
            h.update(b"\x00img\x00")
            h.update(p.image_id.encode() if p.image_id else hashlib.sha256(p.image).digest())
    return h.hexdigest()


def cache_key(digest: str, model: str) -> str:
    return hashlib.sha256(f"{digest}|{PROMPT_VERSION}|{model}|{SCHEMA_VERSION}".encode()).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _result_json(res: ExtractionResult, **extra: Any) -> str:
    d = res.model_dump(exclude={"raw_text"}) | extra
    return json.dumps(d, ensure_ascii=False, indent=1)


class DiskCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> ExtractionResult | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return ExtractionResult.model_validate_json(p.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def put(self, key: str, res: ExtractionResult) -> None:
        _atomic_write(self._path(key), _result_json(res))

    def stats(self) -> tuple[int, int]:
        files = list(self.root.glob("*/*.json")) if self.root.exists() else []
        return len(files), sum(f.stat().st_size for f in files)

    def clear(self) -> int:
        n = 0
        for f in self.root.glob("*/*.json") if self.root.exists() else []:
            f.unlink()
            n += 1
        return n


@dataclass
class LedgerEntry:
    ts: float
    provider: str
    model: str
    schema_name: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_vnd: float


def _ledger_row(d: dict[str, Any]) -> dict[str, Any]:
    """Bản ghi cũ (trước khi chuyển sang VND) có cost_usd; khi đó chưa có đơn giá nên luôn = 0."""
    if "cost_usd" in d:
        d["cost_vnd"] = d.pop("cost_usd")
    return d


class Ledger:
    """Sổ chi phí các lần gọi API THẬT, 1 file JSONL / ngày (UTC). Dùng để chặn ngân sách ngày."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, day: date) -> Path:
        return self.root / f"{day.isoformat()}.jsonl"

    def add(self, entry: LedgerEntry) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._path(datetime.fromtimestamp(entry.ts, UTC).date()).open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def entries(self, day: date | None = None) -> list[LedgerEntry]:
        p = self._path(day or datetime.now(UTC).date())
        if not p.exists():
            return []
        return [
            LedgerEntry(**_ledger_row(json.loads(line)))
            for line in p.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def spent(self, day: date | None = None) -> float:
        return sum(e.cost_vnd for e in self.entries(day))


class Fixtures:
    """Phản hồi thật đã ghi (--record) để mock phát lại. Tên file theo digest (không phụ thuộc model)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, digest: str, schema_name: str) -> Path:
        return self.root / f"{schema_name}_{digest[:20]}.json"

    def load(self, digest: str, schema_name: str) -> dict[str, Any] | None:
        p = self.path(digest, schema_name)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def save(self, digest: str, req: ExtractionRequest, res: ExtractionResult, label: str | None) -> Path:
        p = self.path(digest, req.schema_name)
        _atomic_write(
            p,
            _result_json(res, request_digest=digest, label=label, recorded_at=time.strftime("%Y-%m-%d")),
        )
        return p
