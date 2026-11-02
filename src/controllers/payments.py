# Intentions: the payment provider's side of payments (modules/payments) -- Polar's webhooks, one endpoint per Polar
# world, so the path says which one sent it (and so which secret checks it and which API it's re-fetched from):
#   POST /payments/webhook            live Polar
#   POST /payments/webhook/sandbox    the Polar sandbox
# Signed (Standard Webhooks headers), no login. 401 on a bad signature; 200 once taken in (the project it's about is
# then fulfilled -- services/launch/fulfilment); 500 makes Polar retry.

from fastapi import APIRouter, HTTPException, Request

from src.modules import payments
from src.services.launch import fulfilment
from src.utils.logging import log_http_error

router = APIRouter()


async def _receive_webhook(provider: payments.Provider, request: Request) -> dict:
    try:
        # Starlette hands header names over lower-cased.
        await fulfilment.handle_payment_webhook(provider, await request.body(), dict(request.headers))
        return {"ok": True}
    except payments.InvalidSignature as error:
        raise HTTPException(status_code=401, detail="INVALID_SIGNATURE") from error
    except Exception as error:
        # A 5xx makes the provider retry later -- which is what a failure to take one in should do.
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/payments/webhook")
async def receive_webhook(request: Request) -> dict:
    return await _receive_webhook(payments.Provider.POLAR, request)


@router.post("/payments/webhook/sandbox")
async def receive_sandbox_webhook(request: Request) -> dict:
    return await _receive_webhook(payments.Provider.POLAR_SANDBOX, request)
