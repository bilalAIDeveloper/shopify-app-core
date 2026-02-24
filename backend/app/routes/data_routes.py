from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, File, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from typing import Optional
import io
from PIL import Image

from app.database.engine import get_db
from app.database.repositories.shop_installation_repository import ShopInstallationRepository
from app.services.shopify_service import ShopifyService
from app.services.embedding_service import embedding_service
from app.services.search_service import search_service
from app.templates import (
    DASHBOARD_HTML, 
    SEARCH_VISUALIZER_HTML,
    generate_product_row, 
    generate_customer_row, 
    generate_order_row
)

router = APIRouter()

# ... existing routes ...

@router.get("/search-visualizer", response_class=HTMLResponse)
async def search_visualizer():
    return HTMLResponse(content=SEARCH_VISUALIZER_HTML)


@router.post("/api/search/visualize")
async def api_visualize_search(
    query: str = Form(""),
    limit: int = Form(10),
    image: Optional[UploadFile] = File(None)
):
    """
    Backend API for the visual debugger.
    Handles text embedding, optional image embedding, and hybrid search.
    """
    text_vector = None
    image_vector = None

    # 1. Text Embedding (if query provided)
    if query:
        text_vector = embedding_service.embed_text(query)

    # 2. Image Embedding (if file uploaded)
    if image:
        content = await image.read()
        pil_img = Image.open(io.BytesIO(content))
        image_vector = embedding_service.embed_image(pil_img)

    # 3. Hybrid Search
    results = search_service.perform_hybrid_search(
        query=query,
        text_vector=text_vector,
        image_vector=image_vector,
        limit=limit
    )

    return {"results": results}

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(shop: str, db: Session = Depends(get_db)):
    # 1. Get Token from DB
    repo = ShopInstallationRepository(db)
    installation = repo.get_by_shop(shop)
    
    if not installation:
        return HTMLResponse(content=f"<h1>Error: No installation found for shop: {shop}</h1>", status_code=404)
    
    # We take the first active installation (usually there is only one per shop)
    install = installation[0]
    token = install.access_token

    # 2. Call Shopify API using Service
    service = ShopifyService(shop_domain=shop, access_token=token)
    
    try:
        products = await service.get_products(limit=5)
        customers = await service.get_customers(limit=5)
        orders = await service.get_orders(limit=5)
    except Exception as e:
        return HTMLResponse(content=f"<h1>Error calling Shopify API: {str(e)}</h1>", status_code=500)

    # 3. Render HTML
    products_html = "".join([generate_product_row(p) for p in products]) or "<div class='text-gray-400 italic p-4'>No products found.</div>"
    customers_html = "".join([generate_customer_row(c) for c in customers]) or "<div class='text-gray-400 italic p-4'>No customers found.</div>"
    orders_html = "".join([generate_order_row(o) for o in orders]) or "<tr><td colspan='5' class='text-center py-4 text-gray-400 italic'>No orders found.</td></tr>"

    html_content = DASHBOARD_HTML.format(
        shop_domain=shop,
        masked_token=token[-4:] if token else "....",
        product_count=len(products),
        customer_count=len(customers),
        order_count=len(orders),
        products_html=products_html,
        customers_html=customers_html,
        orders_html=orders_html
    )

    return HTMLResponse(content=html_content)


# ── Product Sync Endpoints ─────────────────────────────────────────────────────

@router.post("/api/products/sync")
async def sync_products(background_tasks: BackgroundTasks, shop: str, db: Session = Depends(get_db)):
    """
    Trigger background ingestion of all products for a shop into Meilisearch.
    Returns immediately with 202 Accepted.
    """
    from ingest_products import ingest_products, get_sync_status, SYNC_STATUS

    repo = ShopInstallationRepository(db)
    installation = repo.get_by_shop(shop)
    if not installation:
        raise HTTPException(status_code=404, detail=f"Shop not found: {shop}")

    # Prevent double-trigger if already processing
    current = get_sync_status(shop)
    if current.get("status") in ("fetching", "processing"):
        return JSONResponse(
            status_code=409,
            content={"detail": "Sync already in progress.", "status": current}
        )

    # Clear old status and kick off background task
    SYNC_STATUS[shop] = {"status": "fetching", "total": 0, "done": 0, "error": ""}
    background_tasks.add_task(ingest_products, shop_domain=shop)

    return JSONResponse(status_code=202, content={"detail": "Product sync started.", "shop": shop})


@router.get("/api/products/sync-status")
async def sync_status(shop: str):
    """
    Poll the current sync status for a shop.
    Returns: { status, total, done, error }
    """
    from ingest_products import get_sync_status
    return JSONResponse(content=get_sync_status(shop))

