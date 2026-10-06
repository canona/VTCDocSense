from fastapi import APIRouter, Request, Response, status

from app.core import readiness

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: process còn sống, không chạm phụ thuộc."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, object]:
    checks = await readiness.run_checks(request.app.state.redis, request.app.state.provider)
    ok = all(v == "ok" for v in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", "checks": checks}
