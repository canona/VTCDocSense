"""Log JSON một dòng/bản ghi, tự gắn job_id/tenant từ contextvars."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)
tenant_var: ContextVar[str | None] = ContextVar("tenant", default=None)

_RESERVED = set(vars(logging.makeLogRecord({})))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if (job_id := job_id_var.get()) is not None:
            payload["job_id"] = job_id
        if (tenant := tenant_var.get()) is not None:
            payload["tenant"] = tenant
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "arq"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
