"""
Product Session Model
─────────────────────
Tracks the products shown to each WhatsApp user (keyed by phone number).
Used for two purposes:
  1. Provide context to the AI about previously shown products
  2. Exclude already-shown handles from the next search
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.engine import Base


class ProductSession(Base):
    __tablename__ = "product_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # WhatsApp phone number (E.164 format without '+', e.g. "923001234567")
    phone_number: Mapped[str] = mapped_column(String(30), index=True, unique=True)

    # JSON list of product dicts: [{title, color, size, price, type, handle, description}, ...]
    # Capped at settings.session_max_products entries (rolling window)
    products_json: Mapped[str] = mapped_column(Text, default="[]")

    # Last activity — used for TTL checks
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
