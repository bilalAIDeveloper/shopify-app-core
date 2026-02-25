"""
Product Session Repository
──────────────────────────
Thin data-access layer for ProductSession.

Stores ALL products ever shown to a phone number (no cap).
Used for:
  1. AI context — "Previously shown products: ..."
  2. Handle exclusion — filter out already-seen handles from next search
"""
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.database.models.product_session import ProductSession


class ProductSessionRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, phone_number: str) -> ProductSession | None:
        return (
            self.db.query(ProductSession)
            .filter(ProductSession.phone_number == phone_number)
            .first()
        )

    def get_products(self, phone_number: str) -> list[dict]:
        """Return all stored products for this phone number, or [] if none."""
        row = self.get(phone_number)
        if row is None:
            return []
        return json.loads(row.products_json or "[]")

    def get_shown_handles(self, phone_number: str) -> list[str]:
        """Return a list of handles already shown to this phone number."""
        products = self.get_products(phone_number)
        return [p["handle"] for p in products if p.get("handle")]

    def append_products(
        self,
        phone_number: str,
        new_products: list[dict],
    ) -> ProductSession:
        """
        Append new_products to this phone's session.
        All products are kept — no rolling window.
        Duplicate handles are deduplicated (last occurrence wins).
        """
        row = self.get(phone_number)
        if row is None:
            row = ProductSession(phone_number=phone_number, products_json="[]")
            self.db.add(row)

        existing: list[dict] = json.loads(row.products_json or "[]")

        # Merge: keep existing, overwrite with new if same handle
        merged: dict[str, dict] = {p["handle"]: p for p in existing if p.get("handle")}
        for p in new_products:
            if p.get("handle"):
                merged[p["handle"]] = p

        # Products without a handle are always appended
        no_handle = [p for p in existing + new_products if not p.get("handle")]

        row.products_json = json.dumps(list(merged.values()) + no_handle)
        row.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(row)
        return row

    def clear(self, phone_number: str) -> None:
        """Remove all stored products for a phone number."""
        row = self.get(phone_number)
        if row:
            self.db.delete(row)
            self.db.commit()
