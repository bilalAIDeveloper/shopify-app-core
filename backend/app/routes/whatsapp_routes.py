import hashlib
import hmac
import json

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session
import base64

from app.config.settings import settings
from app.database.engine import get_db
from app.database.repositories.shop_installation_repository import ShopInstallationRepository
from app.utils.logger import get_logger
from app.services.ai_service import ai_service

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])
logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────
# Signature Verification Helper
# ─────────────────────────────────────────────────────────────

def verify_wa_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """
    Verify the x-wa-signature HMAC-SHA256 sent by the WhatsApp Platform.
    The HMAC is computed over the raw request body using the shared secret.
    """
    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    # Use compare_digest to prevent timing attacks
    return hmac.compare_digest(expected, signature)

async def send_product_messages(api_key: str, phone_number: str, products: list):
    """
    Background task to send up to 3 separate API calls for each product.
    Resolves the image to a base64 string and formats the text per product.
    """
    if not products or not api_key:
        return
        
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Take at most 3 products
        for p in products[:3]:
            image_url = p.get("image_url")
            if not image_url:
                continue
                
            # 1. Download image and convert to Base64
            try:
                img_res = await client.get(image_url)
                img_res.raise_for_status()
                b64_data = base64.b64encode(img_res.content).decode("utf-8")
            except Exception as e:
                logger.error(f"Failed to download image {image_url}: {e}")
                continue
                
            # 2. Format custom caption
            title = p.get("title", "Product")
            price = p.get("price", "N/A")
            handle = p.get("handle", "")
            url = f"https://ismailsclothing.com/products/{handle}"
            
            content = f"*{title}*\n\n*PKR {price}*\n\n{url}"
            
            # 3. Fire-and-forget WA platform dispatch
            payload = {
                "phoneNumber": phone_number,
                "content": content,
                "media": {
                    "type": "IMAGE",
                    "data": b64_data
                }
            }
            
            try:
                headers = {
                    "x-api-key": api_key,
                    "Content-Type": "application/json"
                }
                send_url = f"{settings.wa_platform_url.rstrip('/')}/api/send-message"
                res = await client.post(send_url, json=payload, headers=headers)
                res.raise_for_status()
            except Exception as e:
                logger.error(f"Failed to send product message via WhatsApp Platform: {e}")


async def send_text_message(api_key: str, phone_number: str, text: str):
    """
    Background task to dispatch a quick text message via the WA Platform.
    Useful for 'Searching...' or interim status updates.
    """
    if not text or not api_key:
        return
        
    async with httpx.AsyncClient(timeout=10.0) as client:
        payload = {
            "phoneNumber": phone_number,
            "content": text
        }
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json"
        }
        send_url = f"{settings.wa_platform_url.rstrip('/')}/api/send-message"
        try:
            res = await client.post(send_url, json=payload, headers=headers)
            res.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send interim text message via WA Platform: {e}")


# ─────────────────────────────────────────────────────────────
# Endpoint 0 — Provision Store (You → WA Platform)
# POST /api/whatsapp/provision
# Called by the frontend "Connect WhatsApp" button.
# ─────────────────────────────────────────────────────────────

@router.post("/provision")
async def provision_store(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Called by the Shopify embedded frontend when the merchant clicks
    "Connect WhatsApp". Calls the WA Platform provisioning endpoint,
    then stores the returned agentId + apiKey in our DB.

    Expected body: { "shop": "mystore.myshopify.com" }
    """
    body = await request.json()
    shop_domain = body.get("shop")

    if not shop_domain:
        raise HTTPException(status_code=400, detail="Missing 'shop' field")

    repo = ShopInstallationRepository(db)
    install = repo.get_offline_by_shop(shop_domain)

    if not install:
        raise HTTPException(
            status_code=404,
            detail=f"No installation found for shop: {shop_domain}. Complete OAuth first.",
        )

    # ── Already provisioned? Return existing state ──────────
    if install.wa_agent_id:
        logger.info("provision — already provisioned for shop=%s agent=%s", shop_domain, install.wa_agent_id)
        return {
            "success": True,
            "already_provisioned": True,
            "wa_status": install.wa_status,
            "wa_agent_id": install.wa_agent_id,
        }

    # ── Build the webhookUrl the WA Platform will POST events to ──
    webhook_base_url = f"{settings.app_base_url.rstrip('/')}/api/whatsapp"

    # ── Call WA Platform provisioning ───────────────────────
    provision_url = f"{settings.wa_platform_url.rstrip('/')}/api/shopify/provision"
    payload = {
        "shopId":     str(install.id),       # Use our internal DB id as shopId
        "domain":     shop_domain,
        "shopName":   shop_domain.split(".")[0],   # Simple name from domain
        "webhookUrl": webhook_base_url,
    }

    logger.info("provision — calling WA Platform for shop=%s url=%s", shop_domain, provision_url)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                provision_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-shopify-secret": settings.wa_platform_shared_secret,
                },
            )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error("provision — WA Platform returned error: %s %s", e.response.status_code, e.response.text)
        raise HTTPException(
            status_code=502,
            detail=f"WhatsApp Platform error: {e.response.status_code}",
        )
    except httpx.RequestError as e:
        logger.error("provision — Could not reach WA Platform: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach WhatsApp Platform")

    # ── Save agentId + apiKey to DB ──────────────────────────
    agent_id = data.get("data", {}).get("agentId")
    api_key  = data.get("data", {}).get("apiKey")
    status   = data.get("data", {}).get("status", "INACTIVE")

    if not agent_id or not api_key:
        logger.error("provision — WA Platform response missing agentId/apiKey: %s", data)
        raise HTTPException(status_code=502, detail="Invalid response from WhatsApp Platform")

    repo.update_wa_provisioning(
        shop_domain=shop_domain,
        wa_agent_id=agent_id,
        wa_api_key=api_key,
        wa_status=status,
    )

    logger.info("provision — ✅ success for shop=%s agent=%s status=%s", shop_domain, agent_id, status)
    return {
        "success": True,
        "already_provisioned": False,
        "wa_status": status,
        "wa_agent_id": agent_id,
        "message": "WhatsApp agent provisioned. QR code will arrive in ~1–3 minutes.",
    }


# ─────────────────────────────────────────────────────────────
# Endpoint: GET /api/whatsapp/agent-status?shop=...
# Used by the frontend to poll current WA connection state + QR.
# ─────────────────────────────────────────────────────────────

@router.get("/agent-status")
def get_agent_status(shop: str, db: Session = Depends(get_db)):
    """
    Returns the current WhatsApp agent state for a shop.
    The frontend polls this to know when to show the QR code or connected state.
    """
    repo = ShopInstallationRepository(db)
    install = repo.get_offline_by_shop(shop)

    if not install:
        raise HTTPException(status_code=404, detail="Shop not found")

    return {
        "wa_agent_id":    install.wa_agent_id,
        "wa_status":      install.wa_status or "NOT_PROVISIONED",
        "wa_phone_number": install.wa_phone_number,
        "wa_qr_code":     install.wa_qr_code,   # data URI — render as <img>
    }


# ─────────────────────────────────────────────────────────────
# Endpoint 1 — Incoming WhatsApp Message  (Platform → You)
# POST /api/whatsapp/messages
# ─────────────────────────────────────────────────────────────

@router.post("/messages")
async def receive_message(
    request: Request,
    background_tasks: BackgroundTasks,
    x_wa_signature: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    Called by the WhatsApp Platform when a customer sends a message.
    The platform expects a reply JSON: { "content": "<reply text>" }
    Return { "content": "" } to send no reply.
    """
    raw_body = await request.body()

    # ── Signature verification ──────────────────────────────
    if settings.wa_platform_shared_secret:
        if not x_wa_signature:
            logger.warning("/messages — missing x-wa-signature header")
            raise HTTPException(status_code=401, detail="Missing signature")
        if not verify_wa_signature(raw_body, x_wa_signature, settings.wa_platform_shared_secret):
            logger.warning("/messages — invalid x-wa-signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(raw_body)

    # ── Extract fields ──────────────────────────────────────
    metadata    = payload.get("metadata", {})
    shop_id     = metadata.get("shopId")
    domain      = metadata.get("domain")
    wa_api_key  = metadata.get("apiKey")  # Now provided securely in the payload!

    message           = payload.get("message", {})
    message_id        = message.get("id")        # Use for deduplication later
    from_number       = message.get("from")
    contact           = message.get("contactName")
    content           = message.get("content", "")
    processed_content = message.get("processedContent")  # WA-generated image caption
    media             = message.get("media")     # None for text-only messages

    # Merge text content with image caption (processedContent) into a single enriched string.
    # This avoids sending the image to OpenAI's vision API — cheaper, faster, and sufficient
    # since the WA platform already describes the image for us.
    if processed_content and processed_content != content:
        if content:
            effective_text = f"{content}\n\n Fine products based on this image description \n [Image description: {processed_content}]"
        else:
            effective_text = f"Fine products based on this image description \n [Image description: {processed_content}]"  
    else:
        effective_text = content

    logger.info(f"Effective text: {effective_text}")

    media_url    = media.get("url") if media else None  # Only used for SigLIP visual embedding
    chat_history = payload.get("chatHistory", [])

    logger.info(
        "/messages — shop=%s | from=%s (%s) | msg_id=%s | content=%r | has_processed=%s | has_media=%s | history_len=%d",
        domain, from_number, contact, message_id, content,
        processed_content is not None, media is not None, len(chat_history)
    )

    # ── AI processing ──────────
    try:
        async def _on_search_start(msg: str):
            if wa_api_key and from_number:
                background_tasks.add_task(send_text_message, wa_api_key, from_number, msg)

        async def _on_products_found(products: list):
            if products and wa_api_key and from_number:
                background_tasks.add_task(send_product_messages, wa_api_key, from_number, products)

        reply = await ai_service.process_whatsapp_message(
            text_content=effective_text,
            media_url=media_url,
            phone_number=from_number,
            chat_history=chat_history,
            on_search_start=_on_search_start,
            on_products_found=_on_products_found
        )

    except Exception as e:
        logger.error(f"Error calling AI service: {e}")
        reply = "I'm sorry, I'm experiencing technical difficulties right now. Please try again later."

    # Returning {"content": reply} replies to the incoming message instantly with the text
    return {"content": reply}


# ─────────────────────────────────────────────────────────────
# Endpoint 2 — QR Code Update  (Platform → You)
# POST /api/whatsapp/qr
# ─────────────────────────────────────────────────────────────

@router.post("/qr")
async def receive_qr(
    request: Request,
    x_wa_signature: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    Called by the WA Platform when a new QR code is generated.
    Triggered ~1–3 min after provisioning or when the session expires.
    """
    raw_body = await request.body()

    # ── Signature verification ──────────────────────────────
    if settings.wa_platform_shared_secret:
        if not x_wa_signature:
            raise HTTPException(status_code=401, detail="Missing signature")
        if not verify_wa_signature(raw_body, x_wa_signature, settings.wa_platform_shared_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(raw_body)

    metadata = payload.get("metadata", {})
    domain   = metadata.get("domain")
    qr_code  = payload.get("qrCode")  # Full data URI

    logger.info("/qr — shop=%s | qr_length=%d", domain, len(qr_code) if qr_code else 0)

    if domain and qr_code:
        ShopInstallationRepository(db).update_wa_qr_code(
            shop_domain=domain,
            wa_qr_code=qr_code,
        )

    return {"received": True}


# ─────────────────────────────────────────────────────────────
# Endpoint 3 — Connection Status Update  (Platform → You)
# POST /api/whatsapp/status
# ─────────────────────────────────────────────────────────────

@router.post("/status")
async def receive_status(
    request: Request,
    x_wa_signature: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    Called by the WA Platform when the WhatsApp connection state changes.

    Events:
      - whatsapp.connecting   → Merchant scanned QR, authenticating
      - whatsapp.connected    → Session active, phoneNumber included
      - whatsapp.disconnected → Session dropped/logged out
      - whatsapp.error        → Auth failure or crash
    """
    raw_body = await request.body()

    # ── Signature verification ──────────────────────────────
    if settings.wa_platform_shared_secret:
        if not x_wa_signature:
            raise HTTPException(status_code=401, detail="Missing signature")
        if not verify_wa_signature(raw_body, x_wa_signature, settings.wa_platform_shared_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(raw_body)

    metadata     = payload.get("metadata", {})
    domain       = metadata.get("domain")
    event        = payload.get("event")
    phone_number = payload.get("phoneNumber")  # Only on whatsapp.connected

    logger.info("/status — shop=%s | event=%s | phone=%s", domain, event, phone_number)

    # ── Map event → DB status ───────────────────────────────
    STATUS_MAP = {
        "whatsapp.connecting":   "CONNECTING",
        "whatsapp.connected":    "ACTIVE",
        "whatsapp.disconnected": "DISCONNECTED",
        "whatsapp.error":        "ERROR",
    }

    wa_status = STATUS_MAP.get(event)
    if wa_status and domain:
        ShopInstallationRepository(db).update_wa_status(
            shop_domain=domain,
            wa_status=wa_status,
            wa_phone_number=phone_number,
        )
        logger.info("/status — DB updated: shop=%s status=%s", domain, wa_status)
    else:
        logger.warning("/status — unknown event=%s for shop=%s", event, domain)

    return {"received": True}
