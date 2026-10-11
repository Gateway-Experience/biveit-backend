# Intentions: the runtime-settings API the FE reads at startup.
#   GET /system-configuration         every setting the caller may see, as {name: value}
#   GET /system-configuration/{name}  one setting's value
# Both WIDEN for a super user: an ordinary caller sees only settings marked public
# (is_private false), a super user sees everything. That's the visibility gate; the separate
# status gate (ENABLED/DISABLED) is applied by the repo on every read regardless of who's asking.
# References: src/repositories/system_configurations (the two gates explained).

import pydash
from fastapi import APIRouter, Depends, HTTPException, Request

from src.repositories import system_configurations
from src.utils.auth import get_maybe_superuser
from src.utils.logging import log_http_error

router = APIRouter()


@router.get("/system-configuration")
async def get_all(request: Request, is_superuser: bool = Depends(get_maybe_superuser)) -> dict:
    """Every visible setting as {name: value} -- the shape the FE wants, since it looks settings up by
    name. Served straight from the repo rather than the service's cache: the cache exists for the
    BACKEND's own reads and doesn't carry the visibility metadata this needs."""
    try:
        settings_rows = await system_configurations.find(include_private=is_superuser)
        return pydash.from_pairs([(row["name"], row.get("value")) for row in settings_rows])
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/system-configuration/{name}")
async def get_one(name: str, request: Request, is_superuser: bool = Depends(get_maybe_superuser)) -> dict:
    """One setting's value. A private setting looks exactly like a missing one to a non-super-user
    (404, not 403) -- knowing a setting EXISTS is itself information they shouldn't have."""
    try:
        row = await system_configurations.get(name, include_private=is_superuser)
        if row is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        return {"value": row.get("value")}
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
