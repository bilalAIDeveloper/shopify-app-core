from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session

from ingest_products import ingest_products

from app.config.settings import settings
from app.database.repositories.shop_installation_repository import ShopInstallationRepository
from app.utils.logger import get_logger
from app.utils.security import is_valid_shop_domain, mask_token, verify_shopify_hmac

logger = get_logger(__name__)


class ShopifyAuthService:
    def __init__(self) -> None:
        # In-memory state store is enough for local testing.
        self._states: dict[str, tuple[str, str, datetime]] = {}

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    def build_install_url(self, shop: str, access_mode: str = "offline") -> str:
        logger.debug("build_install_url called — shop=%s access_mode=%s", shop, access_mode)

        if not is_valid_shop_domain(shop):
            logger.warning("build_install_url rejected — invalid shop domain: %s", shop)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid shop domain format.",
            )
        if access_mode not in {"offline", "online"}:
            logger.warning(
                "build_install_url rejected — invalid access_mode=%s for shop=%s",
                access_mode,
                shop,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="access_mode must be either offline or online.",
            )

        state = token_urlsafe(24)
        self._states[state] = (shop, access_mode, datetime.now(timezone.utc))
        self._cleanup_expired_states()

        params: dict[str, str] = {
            "client_id": settings.shopify_api_key,
            "scope": settings.shopify_scopes,
            "redirect_uri": settings.redirect_uri,
            "state": state,
        }
        if access_mode == "online":
            params["grant_options[]"] = "per-user"

        query = urlencode(params)
        install_url = f"https://{shop}/admin/oauth/authorize?{query}"

        logger.info(
            "Install URL built — shop=%s access_mode=%s active_states=%d",
            shop,
            access_mode,
            len(self._states),
        )
        return install_url

    # ------------------------------------------------------------------
    # Callback / token exchange
    # ------------------------------------------------------------------

    async def handle_callback(self, query_params: dict[str, str], db: Session) -> tuple[str, str]:
        logger.debug("handle_callback called — params_keys=%s", sorted(query_params.keys()))

        required = {"shop", "code", "state", "hmac"}
        missing = required - query_params.keys()
        if missing:
            logger.warning("handle_callback rejected — missing params: %s", sorted(missing))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing required Shopify callback parameters.",
            )

        shop = query_params["shop"]

        if not is_valid_shop_domain(shop):
            logger.warning("handle_callback rejected — invalid shop domain: %s", shop)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid shop domain format.",
            )

        if not verify_shopify_hmac(query_params, settings.shopify_api_secret):
            logger.warning("handle_callback rejected — HMAC validation failed for shop=%s", shop)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="HMAC validation failed.",
            )

        logger.debug("HMAC validated successfully for shop=%s", shop)

        state = query_params["state"]
        expected = self._states.pop(state, None)
        if not expected:
            logger.warning(
                "handle_callback rejected — unknown or already-consumed state for shop=%s", shop
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired state.",
            )

        expected_shop, access_mode, created_at = expected

        if expected_shop != shop:
            logger.warning(
                "handle_callback rejected — state/shop mismatch: state_shop=%s callback_shop=%s",
                expected_shop,
                shop,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="State/shop mismatch detected.",
            )

        age_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()
        if age_seconds > settings.state_ttl_seconds:
            logger.warning(
                "handle_callback rejected — state expired: shop=%s age_seconds=%.1f ttl=%d",
                shop,
                age_seconds,
                settings.state_ttl_seconds,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="State expired.",
            )

        logger.debug(
            "State validated — shop=%s access_mode=%s age_seconds=%.1f",
            shop,
            access_mode,
            age_seconds,
        )

        # --- Token exchange ---
        token_url = f"https://{shop}/admin/oauth/access_token"
        logger.info("Exchanging code for access token — shop=%s url=%s", shop, token_url)

        token_payload = {
            "client_id": settings.shopify_api_key,
            "client_secret": settings.shopify_api_secret,
            "code": query_params["code"],
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Shopify requires application/x-www-form-urlencoded (not JSON)
            response = await client.post(
                token_url,
                data=token_payload,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Accept": "application/json"},
            )

        logger.debug(
            "Token exchange response — shop=%s http_status=%d", shop, response.status_code
        )

        if response.status_code >= 400:
            logger.error(
                "Token exchange failed — shop=%s http_status=%d body=%.200s",
                shop,
                response.status_code,
                response.text,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Token exchange failed with status {response.status_code}.",
            )

        data = response.json()
        access_token = data.get("access_token")
        if not access_token:
            logger.error(
                "Token exchange succeeded but access_token missing in response — shop=%s", shop
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Shopify token response missing access_token.",
            )

        scope            = data.get("scope", "")
        associated_user  = data.get("associated_user") or {}
        associated_user_id = str(associated_user.get("id")) if associated_user.get("id") else None

        # ── Verify granted scopes match what we requested ─────────────────────────
        # Shopify docs: a user can alter the scope during the authorize step,
        # so always verify all required scopes were actually granted.
        required_scopes = set(settings.shopify_scopes.split(","))
        granted_scopes  = set(scope.split(",")) if scope else set()
        missing_scopes  = required_scopes - granted_scopes
        if missing_scopes:
            logger.warning(
                "Scope mismatch — shop=%s required=%s granted=%s missing=%s",
                shop, required_scopes, granted_scopes, missing_scopes,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Merchant did not grant required scopes: {', '.join(sorted(missing_scopes))}",
            )
        logger.debug("Scope verified — all required scopes granted: %s", granted_scopes)

        # ── Log everything Shopify returned (token is masked) ──────────────────
        logger.info("─" * 60)
        logger.info("SHOPIFY TOKEN RECEIVED")
        logger.info("  shop            : %s", shop)
        logger.info("  access_mode     : %s", access_mode)
        logger.info("  scope           : %s", scope)
        logger.info("  token (masked)  : %s", mask_token(access_token))
        logger.info("  token length    : %d chars", len(access_token))
        if associated_user:
            logger.info("  associated_user : id=%s  email=%s  name=%s %s",
                associated_user.get("id"),
                associated_user.get("email", "(none)"),
                associated_user.get("first_name", ""),
                associated_user.get("last_name", ""),
            )
        else:
            logger.info("  associated_user : (none — offline token)")
        # Log any extra fields Shopify included (e.g. expires_in for online tokens)
        extra_keys = {k for k in data if k not in {"access_token", "scope", "associated_user"}}
        for key in sorted(extra_keys):
            logger.info("  %-16s: %s", key, data[key])
        logger.info("─" * 60)

        # ── Save to database ───────────────────────────────────────────────────
        logger.info("Saving installation to DB — shop=%s access_mode=%s", shop, access_mode)
        repo = ShopInstallationRepository(db)
        repo.upsert(
            shop_domain=shop,
            access_mode=access_mode,
            access_token=access_token,
            scope=scope,
            associated_user_id=associated_user_id,
        )

        logger.info("─" * 60)
        logger.info("OAUTH FLOW COMPLETE ✓")
        logger.info("  shop        : %s", shop)
        logger.info("  access_mode : %s", access_mode)
        logger.info("  scope       : %s", scope)
        logger.info("  DB saved    : YES")
        logger.info("─" * 60)
        return shop, access_mode

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Token Exchange (Managed Installation / Embedded Apps)
    # ------------------------------------------------------------------

    async def exchange_token(
        self, *, id_token: str, shop: str, db: Session, background_tasks: BackgroundTasks = None
    ) -> str:
        """Exchange a Shopify session token (id_token JWT) for a permanent
        offline access token using the Token Exchange grant type.

        This is the modern flow for embedded apps using Managed Installation.
        Shopify sends the id_token on every GET / request — no /auth/callback needed.
        """
        logger.info("─" * 60)
        logger.info("TOKEN EXCHANGE STARTING")
        logger.info("  shop : %s", shop)
        logger.info("─" * 60)

        token_url = f"https://{shop}/admin/oauth/access_token"
        payload = {
            "client_id":           settings.shopify_api_key,
            "client_secret":       settings.shopify_api_secret,
            "grant_type":          "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token":       id_token,
            "subject_token_type":  "urn:ietf:params:oauth:token-type:id_token",
            # Request a permanent offline token (no expiry)
            "requested_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                token_url,
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept":       "application/json",
                },
            )

        logger.debug(
            "Token exchange response — shop=%s http_status=%d",
            shop, response.status_code,
        )

        if response.status_code >= 400:
            logger.error(
                "Token exchange failed — shop=%s status=%d body=%.300s",
                shop, response.status_code, response.text,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Shopify token exchange failed.",
            )

        data: dict = response.json()
        access_token = data.get("access_token", "")

        if not access_token:
            logger.error("Token exchange returned no access_token — shop=%s", shop)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="No access_token in Shopify token exchange response.",
            )

        scope = data.get("scope", "")

        logger.info("─" * 60)
        logger.info("TOKEN EXCHANGE COMPLETE ✓")
        logger.info("  shop           : %s", shop)
        logger.info("  token (masked) : %s", mask_token(access_token))
        logger.info("  scope          : %s", scope)
        logger.info("─" * 60)

        # Save the offline token to the database
        repo = ShopInstallationRepository(db)
        repo.upsert(
            shop_domain=shop,
            access_mode="offline",
            access_token=access_token,
            scope=scope,
            associated_user_id=None,
        )
        logger.info("Token exchange — offline token saved to DB for shop=%s", shop)
        if background_tasks:
            logger.info("Triggering background product ingestion for shop=%s", shop)
            background_tasks.add_task(ingest_products, shop_domain=shop)
        else:
            logger.warning("BackgroundTasks not provided; skipping ingestion trigger for shop=%s", shop)
            
        return access_token

    def _cleanup_expired_states(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [
            key
            for key, (_, _, created_at) in self._states.items()
            if now - created_at > timedelta(seconds=settings.state_ttl_seconds)
        ]
        if expired:
            logger.debug("Cleaned up %d expired OAuth state(s).", len(expired))
        for key in expired:
            self._states.pop(key, None)


shopify_auth_service = ShopifyAuthService()
