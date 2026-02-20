# FastAPI Shopify Auth Backend

Minimal, modular FastAPI backend for Shopify app install and OAuth callback verification.

## Stack
- FastAPI
- SQLAlchemy
- SQLite (file-based, for local testing)
- Meilisearch (for full-text and vector search)

## Project structure
- `app/main.py`: app bootstrap + router wiring
- `app/controllers/`: controller layer (MVC)
- `app/services/`: Shopify OAuth business logic
- `app/database/models/`: SQLAlchemy models
- `app/database/repositories/`: DB access layer
- `app/routes/`: HTTP route mapping
- `app/utils/`: security helpers
- `app/config/`: environment settings

## Run the server

### Windows (PowerShell)
1. Go to backend folder:
   - `cd "c:\Company project\shopify-store-app\backend"`
2. Create and activate virtual environment:
   - `python -m venv .venv`
   - `.venv\Scripts\Activate.ps1`
3. Install dependencies:
   - `pip install -r requirements.txt`
4. Create env file:
   - `Copy-Item .env.example .env`
   - Update `.env` with real Shopify values.
5. Start server:
   - `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`

### macOS/Linux
1. Go to backend folder:
   - `cd "/path/to/shopify-store-app/backend"`
2. Create and activate virtual environment:
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
3. Install dependencies:
   - `pip install -r requirements.txt`
4. Create env file:
   - `cp .env.example .env`
   - Update `.env` with real Shopify values.
5. Start server:
   - `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`

### Useful commands
- Run on another port:
  - `uvicorn app.main:app --reload --port 9000`
- Health check:
  - `http://127.0.0.1:8000/health`

## Request logging
The app now logs every request with:
- Method, path, query params
- Headers (sensitive headers masked)
- Request body (truncated)
- Response status and duration

Optional env vars:
- `LOG_LEVEL` (default: `INFO`)
- `REQUEST_LOG_BODY_LIMIT` (default: `4000`)

## Required Shopify app settings
Use your ngrok URL as base URL.

- App URL:
  - `https://<your-ngrok-domain>`
- Allowed redirection URL:
  - `https://<your-ngrok-domain>/auth/callback`

## Test install flow
1. Start API locally and ngrok tunnel.
2. Open:
   - `https://<your-ngrok-domain>/auth/install?shop=<store>.myshopify.com&access_mode=offline`
3. Approve install on Shopify.
4. Shopify redirects to `/auth/callback`.
5. Verify saved connection:
   - `GET /auth/shops/<store>.myshopify.com`

## Notes on online + offline tokens
For testing both modes, run install twice:
- Offline:
  - `/auth/install?shop=<store>.myshopify.com&access_mode=offline`
- Online:
  - `/auth/install?shop=<store>.myshopify.com&access_mode=online`

Each mode is stored separately in SQLite via unique `(shop_domain, access_mode)`.
