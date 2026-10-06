from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core import readiness


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(*_: object) -> None:
        return None

    for name in ("check_db", "check_redis", "check_worker"):
        monkeypatch.setattr(readiness, name, ok)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"] == {"db": "ok", "redis": "ok", "worker": "ok", "provider": "ok"}


def test_readyz_degraded(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(*_: object) -> None:
        return None

    async def boom(*_: object) -> None:
        raise ConnectionError("db down")

    monkeypatch.setattr(readiness, "check_db", boom)
    monkeypatch.setattr(readiness, "check_redis", ok)
    monkeypatch.setattr(readiness, "check_worker", ok)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["db"].startswith("error: ConnectionError")
