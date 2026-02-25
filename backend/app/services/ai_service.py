import asyncio
import json
from typing import List, Dict, Any, Optional

from openai import AsyncOpenAI
from app.config.settings import settings
from app.utils.logger import get_logger
from app.services.embedding_service import embedding_service
from app.services.search_service import search_service
from app.prompts.whatsapp_prompts import SYSTEM_PROMPT, SEARCH_TOOL_SCHEMA
from app.database.engine import SessionLocal
from app.database.repositories.product_session_repository import ProductSessionRepository

logger = get_logger(__name__)


class AIService:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def process_whatsapp_message(
        self,
        text_content: str,
        media_url: Optional[str] = None,
        phone_number: Optional[str] = None,
        chat_history: Optional[List[Dict[str, str]]] = None,
        on_search_start: Optional['Callable[[str], Awaitable[None]]'] = None,
        on_products_found: Optional['Callable[[list], Awaitable[None]]'] = None
    ) -> str:
        """
        Main entry point for handling an incoming WhatsApp message with AI.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

        # ── Inject previously shown products into system prompt ───────────────────
        if phone_number:
            with SessionLocal() as db:
                repo = ProductSessionRepository(db)
                prev = repo.get_products(phone_number)

            # Take the N most recent entries and format like format_products_for_ai
            recent = prev[-settings.session_context_limit:] if prev else []
            if recent:
                prev_lines = [
                    "\n\n[Products previously shown to this customer "
                    f"(use to answer follow-up questions like 'how much was the first one?' "
                    f"or 'show me more like item 2'):"
                ]
                for i, p in enumerate(recent, 1):
                    price = p.get("price")
                    price_str = f"PKR {price:,.0f}" if price is not None else "N/A"
                    desc = (p.get("description") or "").strip()
                    prev_lines.append(f"{i}. {p.get('title', '?')}")
                    prev_lines.append(f"   Category    : {p.get('type') or 'N/A'}")
                    prev_lines.append(f"   Color       : {p.get('color') or 'N/A'}")
                    prev_lines.append(f"   Size        : {p.get('size') or 'N/A'}")
                    prev_lines.append(f"   Price       : {price_str}")
                    if desc:
                        prev_lines.append(f"   Description : {desc}")
                    prev_lines.append("")
                prev_lines.append("Do not re-present these unless the customer explicitly asks.]")
                messages[0]["content"] += "\n".join(prev_lines)
                logger.info(f"📌 Injected {len(recent)} recent products into system prompt")

        # Inject chat history if provided
        if chat_history:
            for msg in chat_history:
                # Ensure we only pass valid roles
                role = msg.get("role")
                if role in ["user", "assistant"]:
                    messages.append({
                        "role": role,
                        "content": msg.get("content", "")
                    })

        # Construct the new user message
        user_msg_content = []
        if text_content:
            user_msg_content.append({"type": "text", "text": text_content})
        
        if media_url:
            # If the user sent an image, we provide the image URL to the Vision model
            user_msg_content.append({
                "type": "image_url",
                "image_url": {"url": media_url}
            })
            if not text_content:
                user_msg_content.append({"type": "text", "text": "Please find products similar to this image."})

        messages.append({"role": "user", "content": user_msg_content})

        try:
            logger.info(f"Sending message to OpenAI for user {phone_number}")
            
            # Initial call to OpenAI
            response = await self.client.chat.completions.create(
                model=settings.chat_model,
                messages=messages,
                tools=[SEARCH_TOOL_SCHEMA],
                tool_choice="auto",
                max_tokens=500
            )

            response_message = response.choices[0].message
            tool_calls = response_message.tool_calls

            # If the model decided to call the tool
            if tool_calls:
                # Add the assistant's request to call the tool to the conversation history
                messages.append(response_message)
                
                for tool_call in tool_calls:
                    if tool_call.function.name == "search_products":
                        # Parse arguments
                        args = json.loads(tool_call.function.arguments)
                        search_query = args.get("search_query", "")
                        color_filter = args.get("color_filter")
                        max_price = args.get("max_price")
                        searching_message = args.get("searching_message", "Give me a moment to look for that! 🔎")
                        
                        logger.info("━" * 60)
                        logger.info("🛠  TOOL CALL: search_products")
                        logger.info(f"   search_query    : {search_query!r}")
                        logger.info(f"   color_filter    : {color_filter!r}")
                        logger.info(f"   max_price       : {max_price!r}")
                        logger.info(f"   searching_msg   : {searching_message!r}")
                        logger.info("━" * 60)
                        
                        # Trigger early notification to WA (e.g. "Hold on, searching...")
                        if on_search_start:
                            await on_search_start(searching_message)

                        # ── Load shown handles for exclusion ───────────────────────
                        shown_handles: list[str] = []
                        if phone_number:
                            with SessionLocal() as db:
                                repo = ProductSessionRepository(db)
                                shown_handles = repo.get_shown_handles(phone_number)
                            logger.info(f"� Session: {len(shown_handles)} handles to exclude")

                        # Execute the search — returns (full_results, ai_context, search_context)
                        full_results, ai_context, search_context = await self._execute_search(
                            text_query=search_query,
                            original_media_url=media_url,
                            color=color_filter,
                            max_price=max_price,
                            exclude_handles=shown_handles,
                        )

                        # ── Save new products to session (with description) ─────────
                        if phone_number and full_results:
                            with SessionLocal() as db:
                                repo = ProductSessionRepository(db)
                                repo.append_products(phone_number, [
                                    {
                                        "title":       r.get("title"),
                                        "color":       r.get("color"),
                                        "size":        r.get("size"),
                                        "price":       r.get("price"),
                                        "handle":      r.get("handle"),
                                        "type":        r.get("type"),
                                        "description": r.get("description", ""),
                                    }
                                    for r in full_results
                                ])
                            logger.info(f"💾 Saved {len(full_results)} products to session for {phone_number}")

                        # Dispatch image cards to WhatsApp using full results (has image_url + handle)
                        if on_products_found:
                            await on_products_found(full_results)

                        messages.append({
                            "tool_call_id": tool_call.id,
                            "role": "tool",
                            "name": "search_products",
                            "content": format_products_for_ai(ai_context, search_context)
                        })

                # Second call to OpenAI to generate the final response using the tool results
                logger.info("Sending tool results back to OpenAI")
                final_response = await self.client.chat.completions.create(
                    model=settings.chat_model,
                    messages=messages,
                    max_tokens=500
                )
                
                return final_response.choices[0].message.content

            else:
                # The model didn't call the tool, just answered directly
                return response_message.content

        except Exception as e:
            logger.error(f"Error processing message with AI: {e}")
            return "I'm sorry, I'm having trouble connecting to my system right now. Please try again later."

    async def _execute_search(
        self,
        text_query: str,
        original_media_url: Optional[str] = None,
        color: Optional[str] = None,
        max_price: Optional[float] = None,
        exclude_handles: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Executes the actual search by interacting with the embedding and search services.
        Result count and score threshold are read from settings (search_top_k, search_min_score).
        exclude_handles: list of product handles to exclude from all search stages.
        """
        text_vector = None
        image_vector = None

        # Build base filter string (color + price)
        filters = []
        if color:
            filters.append(f'color = "{color.upper()}"')
        if max_price is not None:
            filters.append(f"price <= {max_price}")

        # Build handle exclusion filter
        exclusion_filter = None
        if exclude_handles:
            quoted = ", ".join(f'"{h}"' for h in exclude_handles)
            exclusion_filter = f"handle NOT IN [{quoted}]"
            logger.info(f"   exclude      : {len(exclude_handles)} handles")

        def _with_exclusion(f_str: Optional[str]) -> Optional[str]:
            """AND the exclusion clause onto whatever filter string is passed in."""
            parts = [p for p in [f_str, exclusion_filter] if p]
            return " AND ".join(parts) if parts else None

        filter_str = " AND ".join(filters) if filters else None

        logger.info("━" * 60)
        logger.info("🔍 SEARCH EXECUTION")
        logger.info(f"   text_query  : {text_query!r}")
        logger.info(f"   color       : {color!r}")
        logger.info(f"   max_price   : {max_price!r}")
        logger.info(f"   filter_str  : {filter_str!r}")
        logger.info(f"   media_url   : {original_media_url!r}")
        logger.info("━" * 60)

        # ── Parallel Embedding ─────────────────────────────────────────────────
        # All embedding calls are blocking (torch / OpenAI HTTP). We offload each
        # to a thread so they run concurrently rather than sequentially.
        async def _embed_openai():
            try:
                return embedding_service.embed_text(text_query)
            except Exception as e:
                logger.error(f"OpenAI text embedding failed: {e}")
                return None

        async def _embed_siglip_text():
            try:
                logger.info(f"SigLIP text → visual embedding: '{text_query}'")
                return await asyncio.to_thread(
                    embedding_service.embed_query_for_image_search, text_query
                )
            except Exception as e:
                logger.error(f"SigLIP text embedding failed: {e}")
                return None

        async def _embed_siglip_image():
            try:
                logger.info(f"SigLIP image embedding from URL: {original_media_url}")
                return await asyncio.to_thread(
                    embedding_service.embed_image, original_media_url
                )
            except Exception as e:
                logger.error(f"SigLIP image embedding failed: {e}")
                return None

        if original_media_url:
            # Run all three in parallel: OpenAI text + SigLIP text + SigLIP image
            text_vector, siglip_text_vector, siglip_image_vector = await asyncio.gather(
                asyncio.to_thread(embedding_service.embed_text, text_query) if text_query else asyncio.sleep(0),
                _embed_siglip_text() if text_query else asyncio.sleep(0),
                _embed_siglip_image()
            )
            # Actual image vector takes priority over text-derived SigLIP vector
            image_vector = siglip_image_vector or siglip_text_vector
        else:
            # Run OpenAI + SigLIP text in parallel (no image provided)
            text_vector, image_vector = await asyncio.gather(
                asyncio.to_thread(embedding_service.embed_text, text_query) if text_query else asyncio.sleep(0),
                _embed_siglip_text() if text_query else asyncio.sleep(0)
            )

        logger.info("━" * 60)
        logger.info("📦 MEILISEARCH PAYLOAD")
        logger.info(f"   query        : {text_query!r}")
        logger.info(f"   filter_str   : {filter_str!r}")
        logger.info(f"   text_vector  : {'✓ ' + str(len(text_vector)) + '-dim' if text_vector else '✗ None'}")
        logger.info(f"   image_vector : {'✓ ' + str(len(image_vector)) + '-dim' if image_vector else '✗ None'}")
        logger.info(f"   limit        : {settings.search_top_k}")
        logger.info("━" * 60)

        # ── Helper: standard search (with optional score threshold) ───────────
        def _search(f_str, min_score: float = None):
            return search_service.perform_hybrid_search(
                query=text_query,
                text_vector=text_vector,
                image_vector=image_vector,
                limit=settings.search_top_k,
                filter_str=f_str,
                ranking_score_threshold=min_score,
            )

        # ── Progressive filter cascade ─────────────────────────────────────────
        search_context = ""   # will describe what filters were actually used

        # Build a human-readable description of what was requested
        requested_parts = []
        if color:     requested_parts.append(f"color={color.upper()}")
        if max_price: requested_parts.append(f"max_price={max_price}")
        requested_desc = ", ".join(requested_parts) if requested_parts else "none"

        # ── Stage 1: Full filter (color + price) ──────────────────────────────
        results = await asyncio.to_thread(_search, filter_str)
        logger.info(f"🔎 Stage 1 (full filter: {filter_str!r}): {len(results)} hits")

        if results:
            search_context = (
                f"Filters requested: {requested_desc}. "
                f"All filters applied — results are exact matches."
            )

        # ── Stage 2: Color only (drop price) ──────────────────────────────────
        if not results and color:
            color_filter_str = f'color = "{color.upper()}"'
            logger.info(f"⚡ Stage 2 (color only: {color_filter_str!r})…")
            results = await asyncio.to_thread(_search, color_filter_str)
            logger.info(f"   color-only: {len(results)} hits")
            if results:
                search_context = (
                    f"Filters requested: {requested_desc}. "
                    f"No products matched both color and price together. "
                    f"Showing products that match the color ({color.upper()}) — price filter was relaxed. "
                    f"These may exceed the requested budget."
                )

        # ── Stage 3: Price only (drop color) ──────────────────────────────────
        if not results and max_price is not None:
            price_filter_str = f"price <= {max_price}"
            logger.info(f"⚡ Stage 3 (price only: {price_filter_str!r})…")
            results = await asyncio.to_thread(_search, price_filter_str)
            logger.info(f"   price-only: {len(results)} hits")
            if results:
                search_context = (
                    f"Filters requested: {requested_desc}. "
                    f"No products matched the requested color ({color.upper() if color else 'N/A'}). "
                    f"Showing products within the budget (≤ {max_price}) — color filter was relaxed. "
                    f"These are not the requested color."
                )

        # ── Stage 4: No filter — pure semantic search with score threshold ────
        if not results:
            logger.info(f"⚡ Stage 4: Unfiltered semantic search (min_score={settings.search_min_score})…")
            results = await asyncio.to_thread(_search, None, settings.search_min_score)
            logger.info(f"   unfiltered: {len(results)} hits (score ≥ {settings.search_min_score})")
            search_context = (
                f"Filters requested: {requested_desc}. "
                f"No products matched any of the requested filters. "
                f"Showing best semantic matches from the full catalog — NOT filtered results. "
                f"Inform the customer that exact matches were not found and offer alternatives."
            )
        # ──────────────────────────────────────────────────────────────────────

        # Cap to top_k (safety net — Stage 2 can still return extras in edge cases)
        results = results[:settings.search_top_k]
        logger.info(f"✅ Final result count (after cap): {len(results)}")

        # Full results for the backend image dispatcher (needs image_url + handle)
        full_results = []
        # Lean results fed to the LLM — no links, no internal fields, just human-readable metadata
        ai_context = []
        for r in results:
            full_results.append({
                "title":        r.get("title"),
                "color":        r.get("color"),
                "size":         r.get("size"),
                "price":        r.get("price"),
                "handle":       r.get("handle"),
                "image_url":    r.get("image_url"),
                "description":  r.get("search_text") or "",
                "_score":       r.get("_score", 1),
                "_rankingScore": round(r.get("_rankingScore", 0.0), 3),
            })
            ai_context.append({
                "title":       r.get("title"),
                "color":       r.get("color"),
                "size":        r.get("size"),
                "price":       r.get("price"),
                "type":        r.get("type") or r.get("product_type"),
                "description": r.get("search_text") or "",
            })

        return full_results, ai_context, search_context


def format_products_for_ai(
    products: list,
    search_context: str = "",
) -> str:
    """
    Convert the ai_context list into a clean, numbered plain-text block.
    Prepends a [Search context] note so the AI knows whether filters were satisfied.
    Previously shown products are injected into the system prompt separately.
    """
    lines = []

    if search_context:
        lines.append(f"[Search context: {search_context}]\n")

    if not products:
        lines.append(search_context or "No matching products were found.")
        return "\n".join(lines)

    lines.append(f"Found {len(products)} new product(s):\n")
    for i, p in enumerate(products, 1):
        price = p.get("price")
        price_str = f"PKR {price:,.0f}" if price is not None else "N/A"
        desc = (p.get("description") or "").strip()

        lines.append(f"{i}. {p.get('title', 'Unknown')}")
        lines.append(f"   Category    : {p.get('type') or 'N/A'}")
        lines.append(f"   Color       : {p.get('color') or 'N/A'}")
        lines.append(f"   Size        : {p.get('size')  or 'N/A'}")
        lines.append(f"   Price       : {price_str}")
        if desc:
            lines.append(f"   Description : {desc}")
        lines.append("")  # blank line between products

    return "\n".join(lines)


ai_service = AIService()
