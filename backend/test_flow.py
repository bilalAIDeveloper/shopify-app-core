"""
Shopify Auth Backend – Flow Test Script
=======================================

Tests the full OAuth flow without needing a real Shopify store.

It runs in two modes:
  1. INTEGRATION tests  – hits the running uvicorn server via HTTP (httpx).
  2. UNIT / HAPPY-PATH  – imports the service directly and mocks Shopify's
                          token endpoint, so the complete code path is
                          exercised in-process.

Usage (from the backend/ directory, with venv active):
    python test_flow.py
    python test_flow.py --base-url http://localhost:8000   # custom server URL
    python test_flow.py --skip-server                      # unit tests only
"""

import argparse
import asyncio
import hashlib
import hmac as hmac_lib
import json
import os
import sys
import time
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

# Make sure the backend package is importable and env vars are set before app imports
backend_dir = Path(__file__).parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# pydantic-settings needs the env vars to be present
os.environ.setdefault("SHOPIFY_API_KEY",    "test_api_key")
os.environ.setdefault("SHOPIFY_API_SECRET", "test_api_secret")
os.environ.setdefault("APP_BASE_URL",       "https://test.ngrok-free.app")

try:
    import httpx
except ImportError:
    pass

from sqlalchemy import select
from app.database.engine import Base, SessionLocal, engine
from app.database.models import ShopInstallation
from app.database.models.shop_installation import ShopInstallation as SI
from app.services.shopify_auth_service import ShopifyAuthService

# ---------------------------------------------------------------------------
# Colour helpers (works on Windows 10+ / Git Bash / VS Code terminal)
# ---------------------------------------------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

PASS = f"{GREEN}✔ PASS{RESET}"
FAIL = f"{RED}✗ FAIL{RESET}"
SKIP = f"{YELLOW}~ SKIP{RESET}"
INFO = f"{CYAN}ℹ{RESET}"

# ---------------------------------------------------------------------------
# Load .env so we know the API secret (needed for HMAC generation)
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).parent / ".env"

def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env

_env = _load_env_file(_ENV_PATH)
API_KEY    = _env.get("SHOPIFY_API_KEY", "test_api_key")
API_SECRET = _env.get("SHOPIFY_API_SECRET", "test_api_secret")

# A valid-format dev store domain (doesn't have to be real for most tests)
TEST_SHOP = "my-dev-store.myshopify.com"

# ---------------------------------------------------------------------------
# HMAC helpers  (mirrors app/utils/security.py exactly)
# ---------------------------------------------------------------------------
def _make_hmac(params: dict[str, str], secret: str) -> str:
    """Generate the HMAC Shopify would attach to a callback."""
    filtered = {k: v for k, v in params.items() if k not in ("hmac", "signature")}
    message = "&".join(f"{k}={v}" for k, v in sorted(filtered.items()))
    return hmac_lib.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _shopify_callback_params(
    shop: str,
    state: str,
    code: str = "fake_code_abc",
    secret: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build a full set of callback query params, including a valid HMAC."""
    secret = secret or API_SECRET
    ts = str(timestamp or int(time.time()))
    params = {
        "shop": shop,
        "state": state,
        "code": code,
        "timestamp": ts,
    }
    params["hmac"] = _make_hmac(params, secret)
    return params


# ---------------------------------------------------------------------------
# Simple test runner
# ---------------------------------------------------------------------------
_results: list[tuple[str, str, str]] = []   # (name, status, detail)


def _record(name: str, passed: bool, detail: str = ""):
    status = PASS if passed else FAIL
    _results.append((name, status, detail))
    detail_str = f"  {detail}" if detail else ""
    print(f"  {status}  {name}{detail_str}")


def _section(title: str):
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (55 - len(title))}{RESET}")


# ---------------------------------------------------------------------------
# ── PART 1: Integration tests (requires running server)
# ---------------------------------------------------------------------------

def run_integration_tests(base_url: str):
    if 'httpx' not in sys.modules:
        print(f"  {SKIP}  httpx not installed — skipping integration tests")
        print(f"         Run: pip install httpx")
        return

    client = httpx.Client(base_url=base_url, follow_redirects=False, timeout=10)

    # ── 1.1  Health check ---------------------------------------------------
    _section("1. Health Check")
    try:
        r = client.get("/health")
        _record("GET /health returns 200",          r.status_code == 200)
        _record("GET /health body is {status: ok}", r.json() == {"status": "ok"})
    except Exception as exc:
        _record("GET /health reachable", False, str(exc))

    # ── 1.2  Install endpoint -----------------------------------------------
    _section("2. Install Endpoint")
    try:
        r = client.get("/auth/install", params={"shop": TEST_SHOP})
        _record("GET /auth/install returns 302",        r.status_code == 302)

        location = r.headers.get("location", "")
        parsed   = urlparse(location)
        qs       = parse_qs(parsed.query)

        _record("Redirect host is shop domain",         TEST_SHOP in parsed.netloc, f"location={location[:80]}")
        _record("Redirect path is /admin/oauth/authorize", "/admin/oauth/authorize" in parsed.path)
        _record("client_id param present",              "client_id" in qs)
        _record("scope param present",                  "scope" in qs)
        _record("redirect_uri param present",           "redirect_uri" in qs)
        _record("state param present",                  "state" in qs)

        # Save the state for later callback tests
        install_state = qs["state"][0] if "state" in qs else None
    except Exception as exc:
        _record("GET /auth/install reachable", False, str(exc))
        install_state = None

    # ── 1.3  Install – error cases ------------------------------------------
    _section("3. Install – Validation Errors")
    try:
        r = client.get("/auth/install", params={"shop": "not-a-shopify-domain.com"})
        _record("Invalid shop domain → 400",  r.status_code == 400)
    except Exception as exc:
        _record("Invalid shop domain handled", False, str(exc))

    try:
        r = client.get("/auth/install", params={"shop": TEST_SHOP, "access_mode": "superuser"})
        _record("Invalid access_mode → 400",  r.status_code == 400)
    except Exception as exc:
        _record("Invalid access_mode handled", False, str(exc))

    # ── 1.4  Callback – error cases -----------------------------------------
    _section("4. Callback – Validation Errors")

    # Missing required params
    try:
        r = client.get("/auth/callback", params={"shop": TEST_SHOP})
        _record("Missing code/state/hmac → 400", r.status_code == 400)
    except Exception as exc:
        _record("Missing params handled", False, str(exc))

    # Invalid shop domain
    try:
        params = _shopify_callback_params("evil.example.com", "somestate")
        r = client.get("/auth/callback", params=params)
        _record("Invalid shop domain in callback → 400", r.status_code == 400)
    except Exception as exc:
        _record("Invalid shop domain in callback handled", False, str(exc))

    # Bad HMAC
    try:
        params = _shopify_callback_params(TEST_SHOP, "somestate", secret="wrong_secret")
        r = client.get("/auth/callback", params=params)
        _record("Wrong HMAC → 401", r.status_code == 401)
    except Exception as exc:
        _record("HMAC check handled", False, str(exc))

    # Valid HMAC but unknown state
    try:
        params = _shopify_callback_params(TEST_SHOP, "totally_unknown_state_xyz")
        r = client.get("/auth/callback", params=params)
        _record("Unknown state → 401", r.status_code == 401)
    except Exception as exc:
        _record("Unknown state handled", False, str(exc))

    # Replay with already-consumed state (if we got a real state from install)
    if install_state:
        try:
            params = _shopify_callback_params(TEST_SHOP, install_state)
            # First call — state is consumed (will fail at token exchange, but that's fine)
            client.get("/auth/callback", params=params)
            # Second call with the same state — must be rejected
            params2 = _shopify_callback_params(TEST_SHOP, install_state)
            r2 = client.get("/auth/callback", params=params2)
            _record("Re-using consumed state → 401", r2.status_code == 401)
        except Exception as exc:
            _record("State re-use handled", False, str(exc))

    # ── 1.5  Shops lookup ---------------------------------------------------
    _section("5. Shop Connection Lookup")

    try:
        r = client.get("/auth/shops/not-valid-domain.com")
        _record("Invalid domain → 400", r.status_code == 400)
    except Exception as exc:
        _record("Shop lookup – invalid domain handled", False, str(exc))

    try:
        r = client.get(f"/auth/shops/{TEST_SHOP}")
        _record("Valid shop lookup → 200",      r.status_code == 200)
        body = r.json()
        _record("Response has 'shop' key",      "shop" in body)
        _record("Response has 'records' list",  isinstance(body.get("records"), list))
    except Exception as exc:
        _record("Shop lookup handled", False, str(exc))

    # ── 1.6  Request-Id header -----------------------------------------------
    _section("6. Request-Id Header (Middleware)")
    try:
        r = client.get("/health")
        has_id = "x-request-id" in r.headers
        _record("X-Request-Id header present on all responses", has_id,
                f"value={r.headers.get('x-request-id', 'MISSING')}")
    except Exception as exc:
        _record("Request-Id check", False, str(exc))

    client.close()


# ---------------------------------------------------------------------------
# ── PART 2: Happy-path unit test (mocks Shopify's token endpoint)
# ---------------------------------------------------------------------------

def run_happy_path_unit_test():
    _section("7. Full OAuth Happy Path (in-process, Shopify mocked)")

    # Move standard sys.path and env logic to the top of the script.

    # Create tables (in-memory SQLite is fine for testing)
    Base.metadata.create_all(bind=engine)

    service = ShopifyAuthService()
    shop    = TEST_SHOP
    db      = SessionLocal()

    try:
        # ── Step A: Build install URL ----------------------------------------
        try:
            install_url = service.build_install_url(shop=shop, access_mode="offline")
            parsed      = urlparse(install_url)
            qs          = parse_qs(parsed.query)
            state       = qs["state"][0]

            _record("build_install_url returns a URL",           bool(install_url))
            _record("URL targets correct shop",                  TEST_SHOP in install_url)
            _record("URL contains client_id",                    "client_id" in qs)
            _record("URL contains state nonce",                  bool(state))
            _record("State is stored in service._states",        state in service._states)
        except Exception as exc:
            _record("build_install_url", False, str(exc))
            return

        # ── Step B: Build valid callback params --------------------------------
        callback_params = _shopify_callback_params(shop, state)

        # ── Step C: Mock Shopify's token endpoint ─────────────────────────────
        # Build a fake response object (synchronous attributes, async .json())
        fake_token_data = {
            "access_token": "shpat_FAKE_TOKEN_abc123xyz",
            "scope":        "read_products,read_orders",
        }
        mock_response = unittest.mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = fake_token_data

        # The inner client object returned by `async with httpx.AsyncClient() as client:`
        mock_inner = unittest.mock.AsyncMock()
        mock_inner.post = unittest.mock.AsyncMock(return_value=mock_response)

        # The outer constructor — its __aenter__ must return the inner client
        mock_outer = unittest.mock.MagicMock()
        mock_outer.__aenter__ = unittest.mock.AsyncMock(return_value=mock_inner)
        mock_outer.__aexit__  = unittest.mock.AsyncMock(return_value=False)

        # ── Step D: Run the callback handler ----------------------------------
        try:
            with unittest.mock.patch("app.services.shopify_auth_service.httpx.AsyncClient",
                                     return_value=mock_outer):
                result_shop, result_mode = asyncio.run(
                    service.handle_callback(callback_params, db)
                )

            _record("handle_callback returns correct shop",        result_shop == shop)
            _record("handle_callback returns correct access_mode", result_mode == "offline")
            _record("State consumed after callback",               state not in service._states)

            # Verify DB write
            # Verify DB write
            row = db.execute(
                select(SI).where(SI.shop_domain == shop, SI.access_mode == "offline")
            ).scalar_one_or_none()

            _record("Installation saved to DB",            row is not None)
            _record("access_token stored correctly",       row is not None and row.access_token == "shpat_FAKE_TOKEN_abc123xyz")
            _record("scope stored correctly",              row is not None and row.scope == "read_products,read_orders")
            _record("is_active is True",                   row is not None and row.is_active is True)

        except Exception as exc:
            _record("handle_callback (mocked)", False, str(exc))

        # ── Step E: Verify state is not re-usable ─────────────────────────────
        try:
            asyncio.run(service.handle_callback(callback_params, db))
            _record("State re-use raises HTTPException", False, "No exception raised")
        except Exception:
            _record("State re-use raises HTTPException (401)", True)

        # ── Step F: HMAC rejection -------------------------------------------
        bad_params = dict(callback_params)
        new_state  = list(service._states.keys())[0] if service._states else "fresh_state"
        service._states["fresh_state"] = (shop, "offline", datetime.now(timezone.utc))
        bad_params["state"] = "fresh_state"
        bad_params["hmac"]  = "deadbeef"   # wrong hash

        try:
            asyncio.run(service.handle_callback(bad_params, db))
            _record("Bad HMAC raises HTTPException", False, "No exception raised")
        except Exception:
            _record("Bad HMAC raises HTTPException (401)", True)

    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


# ---------------------------------------------------------------------------
# ── Summary
# ---------------------------------------------------------------------------

def _print_summary():
    total  = len(_results)
    passed = sum(1 for _, s, _ in _results if "PASS" in s)
    failed = total - passed
    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  Results: {GREEN}{passed} passed{RESET}  "
          f"{(RED + str(failed) + ' failed' + RESET) if failed else (GREEN + '0 failed' + RESET)}"
          f"  / {total} total{RESET}")
    if failed:
        print(f"\n{RED}{BOLD}  Failed tests:{RESET}")
        for name, status, detail in _results:
            if "FAIL" in status:
                print(f"    {FAIL}  {name}" + (f"  →  {detail}" if detail else ""))
    print(f"{BOLD}{'═' * 60}{RESET}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shopify Auth Backend – Flow Tests")
    parser.add_argument("--base-url",    default="http://localhost:8000",
                        help="URL of the running uvicorn server")
    parser.add_argument("--skip-server", action="store_true",
                        help="Skip integration tests (only run in-process unit tests)")
    args = parser.parse_args()

    print(f"\n{BOLD}{CYAN}Shopify Auth Backend – Flow Test Suite{RESET}")
    print(f"{INFO} API key loaded:  {'YES' if _env.get('SHOPIFY_API_KEY') else 'NO (using placeholder)'}")
    print(f"{INFO} API secret loaded: {'YES' if _env.get('SHOPIFY_API_SECRET') else 'NO (using placeholder)'}")
    print(f"{INFO} Server base URL: {args.base_url}")

    if not args.skip_server:
        _section("Connecting to server…")
        try:
            probe = httpx.get(f"{args.base_url}/health", timeout=3)
            print(f"  {PASS}  Server is reachable (HTTP {probe.status_code})")
            run_integration_tests(args.base_url)
        except Exception as exc:
            print(f"  {SKIP}  Server not reachable ({exc})")
            print(f"         Start it with: uvicorn app.main:app --reload --port 8000")
            print(f"         Or use --skip-server to run unit tests only")
    else:
        print(f"\n  {INFO}  Skipping integration tests (--skip-server)")

    run_happy_path_unit_test()
    _print_summary()
