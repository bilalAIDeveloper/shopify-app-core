
import asyncio
import logging
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from app.config.settings import settings
from app.database.engine import get_db
from app.database.repositories.shop_installation_repository import ShopInstallationRepository
from app.services.embedding_service import embedding_service
from app.services.search_service import search_service
from app.services.shopify_service import ShopifyService
from app.utils.logger import get_logger

logging.basicConfig(level=logging.INFO)
logger = get_logger(__name__)

# ── In-memory progress tracker ─────────────────────────────────────────────────
# Maps shop_domain → progress dict (reset on each sync)
SYNC_STATUS: Dict[str, Dict[str, Any]] = {}

# Keywords used to detect Color / Size option names (case-insensitive)
COLOR_KEYWORDS = {"color", "colour", "color name", "shade"}
SIZE_KEYWORDS  = {"size", "dimensions", "pack size", "shoe size", "clothing size"}


def _strip_html(html: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", " ", html or "").strip()


def _extract_options(product: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    Extract color and size option values from a Shopify product.
    Shopify product options look like:
        [{"name": "Color", "values": ["Red", "Blue"]}, ...]
    """
    colors: List[str] = []
    sizes:  List[str] = []

    for option in product.get("options", []):
        name_lower = option.get("name", "").lower().strip()
        values     = [v for v in option.get("values", []) if v]

        if any(kw in name_lower for kw in COLOR_KEYWORDS):
            colors = values
        elif any(kw in name_lower for kw in SIZE_KEYWORDS):
            sizes = values

    return colors, sizes


def _price_range(product: Dict[str, Any]) -> Tuple[float, float]:
    """Return (price_min, price_max) across all variants."""
    prices = []
    for variant in product.get("variants", []):
        try:
            prices.append(float(variant.get("price") or 0))
        except (ValueError, TypeError):
            pass
    if not prices:
        return 0.0, 0.0
    return min(prices), max(prices)


def _main_image_url(product: Dict[str, Any]) -> Optional[str]:
    """Return the first image URL of the product, if any."""
    images = product.get("images", [])
    if images:
        return images[0].get("src")
    return None


def _is_available(product: Dict[str, Any]) -> bool:
    """True if at least one variant is available for purchase."""
    for variant in product.get("variants", []):
        if variant.get("available", False):
            return True
    return False


async def ingest_products(shop_domain: str):
    """
    Full ingestion pipeline:
       1. Fetch all products from Shopify.
       2. For each product:
          a. Extract metadata (colors, sizes, price, category, etc.)
          b. Embed title+description → text vector  (OpenAI)
          c. Embed main product image → image vector (SigLIP)
       3. Upload all documents to Meilisearch (single 'products' index).
    """
    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY not set — aborting ingestion.")
        _update_status(shop_domain, "error", error="OPENAI_API_KEY is not set")
        return

    _update_status(shop_domain, "fetching", total=0, done=0)

    # ── Get access token ──────────────────────────────────────────────────────
    db           = next(get_db())
    repo         = ShopInstallationRepository(db)
    shop_install = repo.get_by_shop(shop_domain)

    if not shop_install or not shop_install.access_token:
        msg = f"No access token found for {shop_domain}"
        logger.error(msg)
        _update_status(shop_domain, "error", error=msg)
        return

    # ── Fetch products ────────────────────────────────────────────────────────
    service = ShopifyService(shop_domain, shop_install.access_token)
    try:
        products = await service.get_products(limit=250)
        logger.info("Fetched %d products from %s", len(products), shop_domain)
    except Exception as exc:
        logger.error("Failed to fetch products: %s", exc)
        _update_status(shop_domain, "error", error=str(exc))
        return

    total = len(products)
    _update_status(shop_domain, "processing", total=total, done=0)

    # ── Configure Meilisearch index (idempotent) ──────────────────────────────
    index_name = settings.meilisearch_index
    search_service.update_settings(index_name, {
        "embedders": {
            "text":  {"source": "userProvided", "dimensions": 1536},
            "image": {"source": "userProvided", "dimensions": 768},
        },
        "filterableAttributes": [
            "shop_domain",
            "vendor",
            "category",
            "colors",
            "sizes",
            "price_min",
            "price_max",
            "available",
        ],
        "searchableAttributes": ["title", "description", "tags", "vendor", "category"],
    })

    documents: List[Dict[str, Any]] = []

    # ── Process each product ──────────────────────────────────────────────────
    for idx, product in enumerate(products, start=1):
        shopify_id  = str(product.get("id", ""))
        doc_id      = f"{shop_domain.replace('.', '_')}_{shopify_id}"
        title       = product.get("title", "")
        raw_desc    = product.get("body_html") or ""
        description = _strip_html(raw_desc)
        vendor      = product.get("vendor", "")
        category    = product.get("product_type", "")
        tags        = product.get("tags", "")
        colors, sizes = _extract_options(product)
        price_min, price_max = _price_range(product)
        available   = _is_available(product)
        image_url   = _main_image_url(product)

        logger.info("[%d/%d] Processing: %s", idx, total, title)

        # ── Text embedding (OpenAI) ───────────────────────────────────────────
        embedding_text = (
            f"Title: {title}. "
            f"Category: {category}. "
            f"Vendor: {vendor}. "
            f"Description: {description}. "
            f"Tags: {tags}."
        )
        try:
            text_vector = embedding_service.embed_text(embedding_text)
        except Exception as exc:
            logger.error("Text embedding failed for %s: %s — skipping.", shopify_id, exc)
            _update_status(shop_domain, "processing", total=total, done=idx)
            continue

        # ── Image embedding (SigLIP) ──────────────────────────────────────────
        image_vector = None
        if image_url:
            try:
                image_vector = embedding_service.embed_image(image_url)
            except Exception as exc:
                logger.warning("Image embedding failed for %s: %s — storing text only.", shopify_id, exc)

        # ── Build document ────────────────────────────────────────────────────
        doc: Dict[str, Any] = {
            "id":          doc_id,
            "shopify_id":  shopify_id,
            "shop_domain": shop_domain,
            "title":       title,
            "description": description,
            "vendor":      vendor,
            "category":    category,
            "tags":        tags,
            "colors":      colors,
            "sizes":       sizes,
            "price_min":   price_min,
            "price_max":   price_max,
            "available":   available,
            "image_url":   image_url,
            "_vectors": {
                "text": text_vector,
                **({"image": image_vector} if image_vector else {}),
            },
        }
        documents.append(doc)
        _update_status(shop_domain, "processing", total=total, done=idx)

    # ── Upload to Meilisearch ─────────────────────────────────────────────────
    if documents:
        task = search_service.add_documents(index_name, documents)
        logger.info("Indexed %d/%d products for %s → task: %s", len(documents), total, shop_domain, task)
        _update_status(shop_domain, "done", total=total, done=len(documents))
    else:
        logger.warning("No documents indexed for %s.", shop_domain)
        _update_status(shop_domain, "done", total=0, done=0)


# ── Status helpers ─────────────────────────────────────────────────────────────

def _update_status(shop_domain: str, status: str, total: int = 0, done: int = 0, error: str = ""):
    SYNC_STATUS[shop_domain] = {
        "status": status,   # fetching | processing | done | error
        "total":  total,
        "done":   done,
        "error":  error,
    }


def get_sync_status(shop_domain: str) -> Dict[str, Any]:
    return SYNC_STATUS.get(shop_domain, {"status": "idle", "total": 0, "done": 0, "error": ""})


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ingest_products.py <shop_domain>")
        sys.exit(1)
    asyncio.run(ingest_products(sys.argv[1]))
