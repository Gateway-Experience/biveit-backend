# Intentions: operational endpoints -- the health probe and the admin config reload. Not part of the
# product API; these serve the deployment (a load balancer's health check) and the operator.

from fastapi import APIRouter, Depends, HTTPException, Request

from src.repositories import check_store_reachable
from src.services import system_config
from src.utils.auth import require_superuser
from src.utils.logging import log_http_error

router = APIRouter()


@router.get("/debug/health")
async def check_health(request: Request) -> dict:
    """Liveness + store reachability. Does a real round trip rather than trusting a driver-side
    "connected" flag -- a flag can say connected while the server is gone. 503 (not 500) on failure,
    so a load balancer reads it as "take me out of rotation", not "this app is broken"."""
    try:
        await check_store_reachable()
        return {"ok": True}
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=503, detail="DB_UNAVAILABLE") from error


@router.post("/system/reload-config")
async def reload_config(request: Request, _: None = Depends(require_superuser)) -> dict:
    """Force the runtime-settings snapshot to refresh now. The snapshot reloads on its own every 15
    minutes; this is for when you don't want to wait out the window after changing a setting."""
    try:
        return {"loaded": await system_config.force_load()}
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
