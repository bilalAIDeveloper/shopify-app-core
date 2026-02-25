from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    shopify_api_key: str
    shopify_api_secret: str
    app_base_url: str
    shopify_scopes: str = "read_products,read_orders"
    shopify_api_version: str = "2025-01"
    sqlite_url: str = "sqlite:///./shopify_auth.db"
    state_ttl_seconds: int = 600
    log_level: str = "INFO"
    request_log_body_limit: int = 4000
    openai_api_key: str = None
    
    # Where to send the merchant after a successful OAuth install.
    # Swap this for your real frontend URL when it's ready.
    post_install_redirect_url: str = "https://bilalportfolio-hazel.vercel.app/"
    
    # Meilisearch
    meilisearch_url: str = "http://localhost:7700"
    meilisearch_master_key: str = "masterKey"
    meilisearch_index: str = "products"

    # AI Models
    chat_model: str = "gpt-4.1-mini"       # Used by the WhatsApp assistant
    caption_model: str = "gpt-4o-mini"     # Used for image captioning during ingestion

    # Search behaviour
    search_top_k: int = 3             # Max products returned per search
    search_min_score: float = 0.5     # Minimum _rankingScore for unfiltered (Stage 4) fallback

    # WhatsApp Platform Integration (platform-wide config)
    wa_platform_url: str = ""
    wa_platform_shared_secret: str = ""
    # NOTE: per-store agentId, apiKey, webhookUrl are stored in the DB per ShopInstallation

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def redirect_uri(self) -> str:
        return f"{self.app_base_url.rstrip('/')}/auth/callback"


settings = Settings()
