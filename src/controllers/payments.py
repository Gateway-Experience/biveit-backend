# Intentions: the payment provider's side of payments (modules/payments), and its stand-in.
#   POST /payments/webhook                    the provider's webhooks (Lemon Squeezy): signed (X-Signature), no
#                                             login. 401 on a bad signature; 200 once taken in (the launch it's
#                                             about is then fulfilled -- services/launch/fulfilment)
#   GET  /payments/mock-checkout/{id}         (auth)  a stand-in checkout, for the FE's fake checkout page
#   POST /payments/mock-checkout/{id}/pay     (auth)  "pay" it: the stand-in's webhooks go through the real path
# The mock ones only exist with settings.payments_mock on (404 otherwise), and only for the checkout's own user.

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from src.config import settings
from src.dto.users import User
from src.modules import payments
from src.services.launch import fulfilment
from src.utils.auth import get_current_user
from src.utils.logging import log_http_error

router = APIRouter()


@router.post("/payments/webhook")
async def receive_webhook(request: Request, x_signature: str = Header("")) -> dict:
    try:
        await fulfilment.handle_payment_webhook(await request.body(), x_signature)
        return {"ok": True}
    except payments.InvalidSignature as error:
        raise HTTPException(status_code=401, detail="INVALID_SIGNATURE") from error
    except Exception as error:
        # A 5xx makes the provider retry later -- which is what a failure to take one in should do.
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


async def _get_own_mock_checkout(checkout_id: str, user: User) -> dict:
    if not settings.payments_mock:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    try:
        checkout = await payments.get_mock_checkout(checkout_id)
    except payments.MockCheckoutNotFound as error:
        raise HTTPException(status_code=404, detail="CHECKOUT_NOT_FOUND") from error
    if checkout["custom_data"].get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="CHECKOUT_NOT_FOUND")
    return checkout


@router.get("/payments/mock-checkout/{checkout_id}")
async def get_mock_checkout(checkout_id: str, request: Request, user: User = Depends(get_current_user)) -> dict:
    """{id, email, item: {product, plan | price_per_year, free_first_year}, price_usd (due today), project_id}."""
    try:
        checkout = await _get_own_mock_checkout(checkout_id, user)
        item = checkout["item"] or {}
        due_today = (
            0
            if item.get("free_first_year")
            else item.get("price_per_year") or payments.PLANS.get(item.get("plan"), {}).get("price_usd")
        )
        return {
            "id": checkout["id"],
            "email": checkout["email"],
            "item": item,
            "price_usd": due_today,
            "project_id": checkout["custom_data"].get("project_id"),
            "domain": checkout["custom_data"].get("domain"),
        }
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/payments/mock-checkout/{checkout_id}/pay")
async def pay_mock_checkout(checkout_id: str, request: Request, user: User = Depends(get_current_user)) -> dict:
    try:
        await _get_own_mock_checkout(checkout_id, user)
        await fulfilment.pay_mock_checkout(checkout_id)
        return {"paid": True}
    except HTTPException:
        raise
    except payments.MockCheckoutNotFound as error:
        raise HTTPException(status_code=404, detail="CHECKOUT_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
