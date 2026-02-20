import httpx
from typing import Dict, Any, List
from app.config.settings import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

class ShopifyService:
    def __init__(self, shop_domain: str, access_token: str):
        self.shop = shop_domain
        self.token = access_token
        self.base_url = f"https://{shop_domain}/admin/api/2024-01"
        self.headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }

    async def get_shop_details(self) -> Dict[str, Any]:
        """Fetch shop details from the REST API."""
        return await self.get_shop_info()

    async def _get(self, endpoint: str) -> dict:
        url = f"{self.base_url}/{endpoint}"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=self.headers)
            if response.status_code != 200:
                logger.error(f"Shopify API Error [{response.status_code}]: {response.text}")
                return {}
            return response.json()

    async def get_shop_info(self) -> dict:
        data = await self._get("shop.json")
        return data.get("shop", {})

    async def get_products(self, limit: int = 50) -> list[dict]:
        data = await self._get(f"products.json?limit={limit}")
        return data.get("products", [])

    async def get_customers(self, limit: int = 5) -> list[dict]:
        data = await self._get(f"customers.json?limit={limit}")
        return data.get("customers", [])

    async def get_orders(self, limit: int = 5) -> list[dict]:
        data = await self._get(f"orders.json?status=any&limit={limit}")
        return data.get("orders", [])
