from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.shop_installation import ShopInstallation
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ShopInstallationRepository:
    def __init__(self, db: Session):
        self.db = db

    def upsert(
        self,
        *,
        shop_domain: str,
        access_mode: str,
        access_token: str,
        scope: str | None,
        associated_user_id: str | None,
    ) -> ShopInstallation:
        stmt = select(ShopInstallation).where(
            ShopInstallation.shop_domain == shop_domain,
            ShopInstallation.access_mode == access_mode,
        )
        existing = self.db.execute(stmt).scalar_one_or_none()

        if existing:
            logger.info(
                "upsert — updating existing installation: shop=%s access_mode=%s scope=%s",
                shop_domain,
                access_mode,
                scope,
            )
            existing.access_token = access_token
            existing.scope = scope
            existing.associated_user_id = associated_user_id
            existing.is_active = True
            self.db.add(existing)
            self.db.commit()
            self.db.refresh(existing)
            logger.debug("upsert — update committed for shop=%s access_mode=%s", shop_domain, access_mode)
            return existing

        logger.info(
            "upsert — creating new installation: shop=%s access_mode=%s scope=%s",
            shop_domain,
            access_mode,
            scope,
        )
        installation = ShopInstallation(
            shop_domain=shop_domain,
            access_mode=access_mode,
            access_token=access_token,
            scope=scope,
            associated_user_id=associated_user_id,
            is_active=True,
        )
        self.db.add(installation)
        self.db.commit()
        self.db.refresh(installation)
        logger.debug("upsert — insert committed for shop=%s access_mode=%s", shop_domain, access_mode)
        return installation

    def get_by_shop(self, shop_domain: str) -> list[ShopInstallation]:
        stmt = (
            select(ShopInstallation)
            .where(ShopInstallation.shop_domain == shop_domain)
            .order_by(ShopInstallation.access_mode.asc())
        )
        results = list(self.db.execute(stmt).scalars().all())
        logger.debug(
            "get_by_shop — shop=%s records_returned=%d", shop_domain, len(results)
        )
        return results

    def get_offline_by_shop(self, shop_domain: str) -> ShopInstallation | None:
        """Return the single offline-mode installation for a shop (most common lookup)."""
        stmt = select(ShopInstallation).where(
            ShopInstallation.shop_domain == shop_domain,
            ShopInstallation.access_mode == "offline",
        )
        return self.db.execute(stmt).scalar_one_or_none()

    # ── WhatsApp helpers ───────────────────────────────────────────────────────

    def update_wa_provisioning(
        self,
        *,
        shop_domain: str,
        wa_agent_id: str,
        wa_api_key: str,
        wa_status: str = "INACTIVE",
    ) -> ShopInstallation | None:
        """Save agentId + apiKey returned by the WA Platform provisioning call."""
        install = self.get_offline_by_shop(shop_domain)
        if not install:
            logger.warning("update_wa_provisioning — no offline install found for shop=%s", shop_domain)
            return None
        install.wa_agent_id = wa_agent_id
        install.wa_api_key = wa_api_key
        install.wa_status = wa_status
        self.db.commit()
        self.db.refresh(install)
        logger.info("update_wa_provisioning — saved agent for shop=%s agent_id=%s", shop_domain, wa_agent_id)
        return install

    def update_wa_status(
        self,
        *,
        shop_domain: str,
        wa_status: str,
        wa_phone_number: str | None = None,
    ) -> ShopInstallation | None:
        """Update WhatsApp connection status (called from /status webhook)."""
        install = self.get_offline_by_shop(shop_domain)
        if not install:
            return None
        install.wa_status = wa_status
        if wa_phone_number is not None:
            install.wa_phone_number = wa_phone_number
        self.db.commit()
        self.db.refresh(install)
        logger.info("update_wa_status — shop=%s status=%s phone=%s", shop_domain, wa_status, wa_phone_number)
        return install

    def update_wa_qr_code(
        self,
        *,
        shop_domain: str,
        wa_qr_code: str,
    ) -> ShopInstallation | None:
        """Overwrite the stored QR code (called from /qr webhook)."""
        install = self.get_offline_by_shop(shop_domain)
        if not install:
            return None
        install.wa_qr_code = wa_qr_code
        self.db.commit()
        self.db.refresh(install)
        logger.info("update_wa_qr_code — shop=%s qr_updated=True", shop_domain)
        return install

