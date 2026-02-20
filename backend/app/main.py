import logging
import sys

from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config.settings import settings
from app.database.engine import Base, engine, get_db
from app.database.models import ShopInstallation  # noqa: F401
from app.database.repositories.shop_installation_repository import ShopInstallationRepository
from app.middleware.request_logging import log_requests_middleware
from app.routes.auth_routes import router as auth_router
from app.routes.data_routes import router as data_router
from app.routes.whatsapp_routes import router as whatsapp_router
from app.services.shopify_auth_service import shopify_auth_service
from app.utils.logger import get_logger
from app.utils.security import verify_shopify_hmac

from sqlalchemy.orm import Session


def _configure_logging() -> None:
    """Set up a structured, human-readable log format for the whole app.

    Format example::

        2026-02-19 10:33:19,123 | INFO     | app.services.shopify_auth_service:42 | Install URL built for my-shop.myshopify.com
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level)
    # Avoid duplicate handlers if create_app() is called more than once (e.g. tests)
    if not root.handlers:
        root.addHandler(handler)
    else:
        for h in root.handlers:
            h.setFormatter(formatter)

    # Quiet down noisy third-party loggers unless we're in DEBUG mode
    if log_level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def create_app() -> FastAPI:
    _configure_logging()

    logger = get_logger(__name__)
    logger.info(
        "Starting Shopify Auth Backend — log_level=%s, db=%s",
        settings.log_level.upper(),
        settings.sqlite_url,
    )

    app = FastAPI(title="Shopify Auth Backend", version="0.1.0")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables verified / created.")

    app.middleware("http")(log_requests_middleware)
    logger.debug("Request-logging middleware registered.")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_model=None)
    async def root(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> Response:
        """Smart entry-point that handles two distinct Shopify request types:

        1. Install trigger (from App Store / install link):
           GET /?shop=xxx&hmac=xxx&timestamp=xxx  (no 'embedded' param)
           → Redirect to /auth/install to start the OAuth flow.

        2. Embedded app load (Managed Installation via Partner Dashboard):
           GET /?embedded=1&shop=xxx&id_token=xxx&host=xxx
           → Exchange id_token for offline access token (first visit only),
             then serve the app UI.
        """
        params   = dict(request.query_params)
        shop     = params.get("shop", "")
        embedded = params.get("embedded", "0")
        hmac     = params.get("hmac", "")

        # ── Case 1: New install trigger from Shopify ────────────────────────────
        # Shopify docs: When a user installs the app, Shopify sends a GET
        # request to the App URL with shop + hmac + timestamp. We detect this
        # by the presence of hmac and the absence of embedded=1.
        if shop and hmac and embedded != "1":
            # OPTIONAL BUT RECOMMENDED: Validate HMAC before redirecting.
            # This ensures only legitimate requests from Shopify trigger the auth flow.
            if not verify_shopify_hmac(params, settings.shopify_api_secret):
                logger.warning("root — HMAC validation failed for initial install request: shop=%s", shop)
                return Response("Unauthorized: Invalid HMAC signature", status_code=401)

            logger.info("root — install trigger detected for shop=%s, redirecting to OAuth", shop)
            return RedirectResponse(
                url=f"/auth/install?shop={shop}",
                status_code=302,
            )

        # ── Case 2: Embedded app load (Managed Installation) ─────────────────────
        # Shopify sends id_token on EVERY load. Exchange it for an offline
        # access token on the FIRST visit (when DB has no token for this shop).
        logger.debug("root — embedded app load for shop=%s", shop)
        host     = params.get("host", "")
        id_token = params.get("id_token", "")
        api_key  = settings.shopify_api_key

        if shop and id_token:
            repo = ShopInstallationRepository(db)
            existing = repo.get_by_shop(shop)
            if not existing:
                # First visit — no token in DB yet. Exchange now.
                logger.info("root — no token found for shop=%s, triggering token exchange", shop)
                try:
                    await shopify_auth_service.exchange_token(
                        id_token=id_token, shop=shop, db=db, background_tasks=background_tasks
                    )
                except Exception as exc:
                    logger.error("root — token exchange failed for shop=%s: %s", shop, exc)
            else:
                logger.debug("root — token already in DB for shop=%s, skipping exchange", shop)
        shop_line = f"<p><strong>Store:</strong> {shop}</p>" if shop else ""
        return HTMLResponse(
            content=f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Shopify WhatsApp App</title>

                <!-- Shopify App Bridge -->
                <script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"
                        crossorigin="anonymous"></script>

                <style>
                    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

                    body {{
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                        background: #f6f6f7;
                        min-height: 100vh;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        padding: 2rem;
                    }}

                    .container {{
                        width: 100%;
                        max-width: 480px;
                        display: flex;
                        flex-direction: column;
                        gap: 1rem;
                    }}

                    /* ── Card ── */
                    .card {{
                        background: #ffffff;
                        border-radius: 12px;
                        box-shadow: 0 2px 12px rgba(0,0,0,0.08);
                        padding: 2rem;
                    }}

                    .card-header {{
                        display: flex;
                        align-items: center;
                        gap: 0.75rem;
                        margin-bottom: 1.25rem;
                    }}

                    .card-header .icon {{
                        width: 44px; height: 44px;
                        background: linear-gradient(135deg, #25d366 0%, #128c7e 100%);
                        border-radius: 10px;
                        display: flex; align-items: center; justify-content: center;
                        font-size: 1.4rem;
                        flex-shrink: 0;
                    }}

                    .card-header h2 {{
                        font-size: 1.1rem;
                        font-weight: 600;
                        color: #1a1a1a;
                    }}

                    .card-header p {{
                        font-size: 0.82rem;
                        color: #777;
                        margin-top: 2px;
                    }}

                    /* ── Status badge ── */
                    .status-row {{
                        display: flex;
                        align-items: center;
                        justify-content: space-between;
                        background: #f9fafb;
                        border-radius: 8px;
                        padding: 0.75rem 1rem;
                        margin-bottom: 1.25rem;
                        border: 1px solid #ebebeb;
                    }}

                    .status-label {{
                        font-size: 0.82rem;
                        color: #666;
                        font-weight: 500;
                    }}

                    .badge {{
                        display: inline-flex;
                        align-items: center;
                        gap: 0.35rem;
                        padding: 0.25rem 0.75rem;
                        border-radius: 999px;
                        font-size: 0.78rem;
                        font-weight: 600;
                    }}

                    .badge.inactive  {{ background: #f3f4f6; color: #6b7280; }}
                    .badge.connecting{{ background: #fef3c7; color: #92400e; }}
                    .badge.active    {{ background: #d1fae5; color: #065f46; }}
                    .badge.error,
                    .badge.disconnected {{ background: #fee2e2; color: #991b1b; }}

                    .dot {{
                        width: 7px; height: 7px;
                        border-radius: 50%;
                        background: currentColor;
                    }}

                    /* ── Button ── */
                    #connectBtn {{
                        width: 100%;
                        padding: 0.85rem;
                        border: none;
                        border-radius: 8px;
                        font-size: 0.95rem;
                        font-weight: 600;
                        cursor: pointer;
                        background: linear-gradient(135deg, #25d366 0%, #128c7e 100%);
                        color: #fff;
                        transition: opacity 0.2s, transform 0.1s;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        gap: 0.5rem;
                    }}

                    #connectBtn:disabled {{
                        opacity: 0.55;
                        cursor: not-allowed;
                    }}

                    #connectBtn:not(:disabled):hover  {{ opacity: 0.9; }}
                    #connectBtn:not(:disabled):active {{ transform: scale(0.98); }}

                    /* ── QR panel ── */
                    #qrPanel {{
                        text-align: center;
                        display: none;
                    }}

                    #qrPanel p {{
                        font-size: 0.85rem;
                        color: #555;
                        margin-bottom: 1rem;
                        line-height: 1.5;
                    }}

                    #qrPanel img {{
                        width: 220px;
                        height: 220px;
                        border-radius: 8px;
                        border: 3px solid #25d366;
                        display: block;
                        margin: 0 auto;
                    }}

                    #qrExpiry {{
                        font-size: 0.76rem;
                        color: #999;
                        margin-top: 0.6rem;
                    }}

                    /* ── Connected panel ── */
                    #connectedPanel {{
                        text-align: center;
                        display: none;
                    }}

                    #connectedPanel .check {{
                        font-size: 3rem;
                        margin-bottom: 0.5rem;
                    }}

                    #connectedPanel h3 {{
                        color: #065f46;
                        font-size: 1.05rem;
                        font-weight: 700;
                    }}

                    #connectedPanel p {{
                        color: #555;
                        font-size: 0.85rem;
                        margin-top: 0.4rem;
                    }}

                    /* ── Error msg ── */
                    #errorMsg {{
                        background: #fee2e2;
                        color: #991b1b;
                        border-radius: 8px;
                        padding: 0.75rem 1rem;
                        font-size: 0.84rem;
                        display: none;
                    }}

                    /* ── Dashboard link ── */
                    .secondary-link {{
                        display: block;
                        text-align: center;
                        text-decoration: none;
                        color: #008060;
                        font-size: 0.85rem;
                        font-weight: 500;
                        padding: 0.6rem;
                        border-radius: 8px;
                        border: 1px solid #c9ede3;
                        background: #f0faf6;
                        transition: background 0.15s;
                    }}

                    .secondary-link:hover {{ background: #e0f5ee; }}

                    /* ── Spinner ── */
                    .spinner {{
                        width: 16px; height: 16px;
                        border: 2px solid rgba(255,255,255,0.4);
                        border-top-color: #fff;
                        border-radius: 50%;
                        animation: spin 0.7s linear infinite;
                        display: none;
                    }}

                    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

                    /* ── Sync card ── */
                    #syncCard {{}}

                    .progress-wrap {{
                        background: #f3f4f6;
                        border-radius: 999px;
                        height: 8px;
                        overflow: hidden;
                        margin: 0.75rem 0 0.4rem;
                        display: none;
                    }}

                    .progress-bar {{
                        height: 100%;
                        background: linear-gradient(90deg, #6366f1 0%, #8b5cf6 100%);
                        border-radius: 999px;
                        transition: width 0.4s ease;
                        width: 0%;
                    }}

                    .progress-label {{
                        font-size: 0.78rem;
                        color: #6b7280;
                        text-align: right;
                        display: none;
                    }}

                    #syncBtn {{
                        width: 100%;
                        padding: 0.85rem;
                        border: none;
                        border-radius: 8px;
                        font-size: 0.95rem;
                        font-weight: 600;
                        cursor: pointer;
                        background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
                        color: #fff;
                        transition: opacity 0.2s, transform 0.1s;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        gap: 0.5rem;
                        margin-top: 0.5rem;
                    }}

                    #syncBtn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
                    #syncBtn:not(:disabled):hover  {{ opacity: 0.9; }}
                    #syncBtn:not(:disabled):active {{ transform: scale(0.98); }}

                    #syncErrorMsg {{
                        background: #fee2e2;
                        color: #991b1b;
                        border-radius: 8px;
                        padding: 0.75rem 1rem;
                        font-size: 0.84rem;
                        display: none;
                        margin-bottom: 0.5rem;
                    }}

                    #syncSuccessMsg {{
                        background: #d1fae5;
                        color: #065f46;
                        border-radius: 8px;
                        padding: 0.75rem 1rem;
                        font-size: 0.84rem;
                        display: none;
                        margin-bottom: 0.5rem;
                        text-align: center;
                    }}
                </style>
            </head>
            <body>
                <div class="container">

                    <!-- WhatsApp Connection Card -->
                    <div class="card">
                        <div class="card-header">
                            <div class="icon">💬</div>
                            <div>
                                <h2>WhatsApp Integration</h2>
                                <p>{shop}</p>
                            </div>
                        </div>

                        <!-- Status row -->
                        <div class="status-row">
                            <span class="status-label">Connection Status</span>
                            <span class="badge inactive" id="statusBadge">
                                <span class="dot"></span>
                                <span id="statusText">Not Connected</span>
                            </span>
                        </div>

                        <!-- Error message -->
                        <div id="errorMsg"></div>

                        <!-- QR code panel -->
                        <div id="qrPanel">
                            <p>📱 Open WhatsApp on your phone → Menu → <strong>Linked Devices</strong> → Scan this QR code</p>
                            <img id="qrImg" src="" alt="WhatsApp QR Code" />
                            <p id="qrExpiry">QR code refreshes automatically every ~30–60 seconds</p>
                        </div>

                        <!-- Connected panel -->
                        <div id="connectedPanel">
                            <div class="check">✅</div>
                            <h3>WhatsApp Connected!</h3>
                            <p id="phoneDisplay">Your store is ready to send &amp; receive messages.</p>
                        </div>

                        <!-- Connect button -->
                        <button id="connectBtn" onclick="connectWhatsApp()">
                            <span class="spinner" id="btnSpinner"></span>
                            <span id="btnText">🔗 Connect WhatsApp</span>
                        </button>
                    </div>

                    <!-- Process Products Card -->
                    <div class="card" id="syncCard">
                        <div class="card-header">
                            <div class="icon" style="background: linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%)">🧠</div>
                            <div>
                                <h2>AI Product Indexing</h2>
                                <p>Embed &amp; index products for smart search</p>
                            </div>
                        </div>

                        <div id="syncErrorMsg"></div>
                        <div id="syncSuccessMsg"></div>

                        <!-- Progress bar (hidden until sync starts) -->
                        <div class="progress-wrap" id="progressWrap">
                            <div class="progress-bar" id="progressBar"></div>
                        </div>
                        <div class="progress-label" id="progressLabel"></div>

                        <button id="syncBtn" onclick="startSync()">
                            <span class="spinner" id="syncSpinner"></span>
                            <span id="syncBtnText">⚡ Process Products</span>
                        </button>
                    </div>

                    <!-- Dashboard link -->
                    <a href="/dashboard?shop={shop}" class="secondary-link">
                        View Data Dashboard →
                    </a>

                </div>

                <!-- App Bridge init -->
                <script>
                    (function () {{
                        try {{
                            const apiKey = "{api_key}";
                            const host   = "{host}";
                            if (!host) {{ console.warn("[AppBridge] No host param"); return; }}
                            shopify.createApp({{ apiKey, host }});
                        }} catch (err) {{
                            console.error("[AppBridge] Init error:", err);
                        }}
                    }})();
                </script>

                <!-- WhatsApp connection logic -->
                <script>
                    const SHOP = "{shop}";
                    let pollInterval = null;

                    // ── Render state ──────────────────────────────────────────
                    function renderStatus(state) {{
                        const badge       = document.getElementById('statusBadge');
                        const statusText  = document.getElementById('statusText');
                        const qrPanel     = document.getElementById('qrPanel');
                        const qrImg       = document.getElementById('qrImg');
                        const connPanel   = document.getElementById('connectedPanel');
                        const phoneDisp   = document.getElementById('phoneDisplay');
                        const btn         = document.getElementById('connectBtn');
                        const btnText     = document.getElementById('btnText');

                        // Hide both panels first
                        qrPanel.style.display   = 'none';
                        connPanel.style.display  = 'none';

                        // Reset badge class
                        badge.className = 'badge';

                        switch (state.wa_status) {{
                            case 'NOT_PROVISIONED':
                            case null:
                            case undefined:
                                badge.classList.add('inactive');
                                statusText.textContent = 'Not Connected';
                                btn.disabled = false;
                                btnText.textContent = '🔗 Connect WhatsApp';
                                break;

                            case 'INACTIVE':
                                badge.classList.add('inactive');
                                statusText.textContent = 'Setting Up...';
                                btn.disabled = true;
                                btnText.textContent = '⏳ Setting up WhatsApp...';
                                startPolling();
                                break;

                            case 'CONNECTING':
                                badge.classList.add('connecting');
                                statusText.textContent = 'Waiting for QR Scan';
                                btn.disabled = true;
                                btnText.textContent = '📷 Scan QR Code Below';
                                if (state.wa_qr_code) {{
                                    qrImg.src = state.wa_qr_code;
                                    qrPanel.style.display = 'block';
                                }}
                                startPolling();
                                break;

                            case 'ACTIVE':
                                badge.classList.add('active');
                                statusText.textContent = 'Connected ✓';
                                btn.disabled = true;
                                btnText.textContent = '✅ WhatsApp Connected';
                                connPanel.style.display = 'block';
                                if (state.wa_phone_number) {{
                                    phoneDisp.textContent = '📞 Connected number: +' + state.wa_phone_number;
                                }}
                                stopPolling();
                                break;

                            case 'DISCONNECTED':
                            case 'ERROR':
                                badge.classList.add('error');
                                statusText.textContent = state.wa_status === 'ERROR' ? 'Error' : 'Disconnected';
                                btn.disabled = false;
                                btnText.textContent = '🔄 Reconnect WhatsApp';
                                stopPolling();
                                break;

                            default:
                                badge.classList.add('inactive');
                                statusText.textContent = state.wa_status;
                        }}
                    }}

                    // ── Connect button click ──────────────────────────────────
                    async function connectWhatsApp() {{
                        const btn     = document.getElementById('connectBtn');
                        const spinner = document.getElementById('btnSpinner');
                        const btnText = document.getElementById('btnText');
                        const errDiv  = document.getElementById('errorMsg');

                        btn.disabled          = true;
                        spinner.style.display = 'block';
                        btnText.textContent   = 'Connecting...';
                        errDiv.style.display  = 'none';

                        try {{
                            const res = await fetch('/api/whatsapp/provision', {{
                                method: 'POST',
                                headers: {{ 'Content-Type': 'application/json' }},
                                body: JSON.stringify({{ shop: SHOP }}),
                            }});

                            const data = await res.json();

                            if (!res.ok) {{
                                throw new Error(data.detail || 'Provisioning failed');
                            }}

                            // Immediately poll to get updated state
                            await pollStatus();
                            startPolling();

                        }} catch (err) {{
                            errDiv.textContent   = '❌ ' + err.message;
                            errDiv.style.display = 'block';
                            btn.disabled         = false;
                            btnText.textContent  = '🔗 Connect WhatsApp';
                        }} finally {{
                            spinner.style.display = 'none';
                        }}
                    }}

                    // ── Polling ───────────────────────────────────────────────
                    async function pollStatus() {{
                        try {{
                            const res  = await fetch('/api/whatsapp/agent-status?shop=' + encodeURIComponent(SHOP));
                            const data = await res.json();
                            if (res.ok) renderStatus(data);
                        }} catch (e) {{
                            console.warn('[WA] Poll failed:', e);
                        }}
                    }}

                    function startPolling() {{
                        if (pollInterval) return;
                        pollInterval = setInterval(pollStatus, 5000);
                    }}

                    function stopPolling() {{
                        if (pollInterval) {{ clearInterval(pollInterval); pollInterval = null; }}
                    }}

                    // ── On load: fetch current status ─────────────────────────
                    if (SHOP) pollStatus();
                </script>

                <!-- Product Sync logic -->
                <script>
                    let syncPollInterval = null;

                    async function startSync() {{
                        const btn       = document.getElementById('syncBtn');
                        const spinner   = document.getElementById('syncSpinner');
                        const btnText   = document.getElementById('syncBtnText');
                        const errDiv    = document.getElementById('syncErrorMsg');
                        const okDiv     = document.getElementById('syncSuccessMsg');

                        btn.disabled          = true;
                        spinner.style.display = 'block';
                        btnText.textContent   = 'Starting...';
                        errDiv.style.display  = 'none';
                        okDiv.style.display   = 'none';

                        showProgress(0, 0);

                        try {{
                            const res = await fetch('/api/products/sync?shop=' + encodeURIComponent(SHOP), {{
                                method: 'POST'
                            }});
                            const data = await res.json();

                            if (res.status === 409) {{
                                // Already running — just start polling
                                btnText.textContent = '⏳ Sync in progress...';
                            }} else if (!res.ok) {{
                                throw new Error(data.detail || 'Failed to start sync');
                            }} else {{
                                btnText.textContent = '⏳ Processing...';
                            }}
                            startSyncPolling();

                        }} catch (err) {{
                            errDiv.textContent   = '❌ ' + err.message;
                            errDiv.style.display = 'block';
                            btn.disabled         = false;
                            btnText.textContent  = '⚡ Process Products';
                            spinner.style.display = 'none';
                        }}
                    }}

                    function showProgress(done, total) {{
                        const wrap  = document.getElementById('progressWrap');
                        const bar   = document.getElementById('progressBar');
                        const label = document.getElementById('progressLabel');

                        wrap.style.display  = 'block';
                        label.style.display = 'block';

                        const pct = total > 0 ? Math.round((done / total) * 100) : 0;
                        bar.style.width     = pct + '%';
                        label.textContent   = total > 0 ? done + ' / ' + total + ' products' : 'Fetching products...';
                    }}

                    async function pollSyncStatus() {{
                        try {{
                            const res  = await fetch('/api/products/sync-status?shop=' + encodeURIComponent(SHOP));
                            const data = await res.json();

                            const btn     = document.getElementById('syncBtn');
                            const spinner = document.getElementById('syncSpinner');
                            const btnText = document.getElementById('syncBtnText');
                            const errDiv  = document.getElementById('syncErrorMsg');
                            const okDiv   = document.getElementById('syncSuccessMsg');

                            showProgress(data.done || 0, data.total || 0);

                            if (data.status === 'done') {{
                                stopSyncPolling();
                                btn.disabled          = false;
                                spinner.style.display = 'none';
                                btnText.textContent   = '🔄 Re-process Products';
                                okDiv.textContent     = '✅ ' + (data.done || 0) + ' products indexed successfully!';
                                okDiv.style.display   = 'block';
                            }} else if (data.status === 'error') {{
                                stopSyncPolling();
                                btn.disabled          = false;
                                spinner.style.display = 'none';
                                btnText.textContent   = '⚡ Process Products';
                                errDiv.textContent    = '❌ Error: ' + (data.error || 'Unknown error');
                                errDiv.style.display  = 'block';
                            }}
                        }} catch (e) {{
                            console.warn('[Sync] Poll failed:', e);
                        }}
                    }}

                    function startSyncPolling() {{
                        if (syncPollInterval) return;
                        syncPollInterval = setInterval(pollSyncStatus, 3000);
                    }}

                    function stopSyncPolling() {{
                        if (syncPollInterval) {{ clearInterval(syncPollInterval); syncPollInterval = null; }}
                    }}

                    // On load: resume polling if sync was already in progress
                    (async function () {{
                        if (!SHOP) return;
                        const res  = await fetch('/api/products/sync-status?shop=' + encodeURIComponent(SHOP));
                        const data = await res.json();
                        if (data.status === 'fetching' || data.status === 'processing') {{
                            document.getElementById('syncBtn').disabled = true;
                            document.getElementById('syncBtnText').textContent = '⏳ Processing...';
                            document.getElementById('syncSpinner').style.display = 'block';
                            showProgress(data.done || 0, data.total || 0);
                            startSyncPolling();
                        }} else if (data.status === 'done') {{
                            showProgress(data.done, data.total);
                            document.getElementById('syncSuccessMsg').textContent = '✅ ' + data.done + ' products indexed.';
                            document.getElementById('syncSuccessMsg').style.display = 'block';
                        }}
                    }})();
                </script>
            </body>
            </html>
            """,
            status_code=200,
        )

    app.include_router(auth_router, prefix="/auth", tags=["auth"])
    logger.info("Auth router mounted at /auth.")

    app.include_router(data_router, tags=["data"])
    logger.info("Data router mounted.")

    app.include_router(whatsapp_router)
    logger.info("WhatsApp webhook router mounted at /api/whatsapp.")

    return app


app = create_app()
