import asyncio
import json
from typing import List, Dict, Any, Optional

from openai import AsyncOpenAI
from app.config.settings import settings
from app.utils.logger import get_logger
from app.services.embedding_service import embedding_service
from app.services.search_service import search_service
from app.prompts.whatsapp_prompts import SYSTEM_PROMPT, SEARCH_TOOL_SCHEMA

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
                model="gpt-4o", # Using 4o-mini as it supports vision and function calling efficiently
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

                        # Execute the search — returns (full_results, ai_context)
                        full_results, ai_context = await self._execute_search(
                            text_query=search_query,
                            original_media_url=media_url, # Pass the original media for hybrid vector search
                            color=color_filter,
                            max_price=max_price
                        )
                        
                        # Dispatch image cards to WhatsApp using full results (has image_url + handle)
                        if on_products_found:
                            await on_products_found(full_results)

                        # Feed only the lean ai_context to the LLM (no links or internal fields)
                        messages.append({
                            "tool_call_id": tool_call.id,
                            "role": "tool",
                            "name": "search_products",
                            "content": json.dumps(ai_context)
                        })

                # Second call to OpenAI to generate the final response using the tool results
                logger.info("Sending tool results back to OpenAI")
                final_response = await self.client.chat.completions.create(
                    model="gpt-4o-mini",
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
        max_price: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Executes the actual search by interacting with the embedding and search services.
        """
        text_vector = None
        image_vector = None

        # Build filter string
        filters = []
        if color:
            filters.append(f'color = "{color.upper()}"')
        if max_price is not None:
            filters.append(f"price <= {max_price}")
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
        logger.info(f"   limit        : 3")
        logger.info("━" * 60)

        # Offload sync Meilisearch search to a thread — keeps the event loop free
        results = await asyncio.to_thread(
            search_service.perform_hybrid_search,
            query=text_query,
            text_vector=text_vector,
            image_vector=image_vector,
            limit=3,
            filter_str=filter_str
        )

        # Full results for the backend image dispatcher (needs image_url + handle)
        full_results = []
        # Lean results fed to the LLM — no links, no internal fields, just human-readable metadata
        ai_context = []
        for r in results:
            full_results.append({
                "title": r.get("title"),
                "color": r.get("color"),
                "size": r.get("size"),
                "price": r.get("price"),
                "handle": r.get("handle"),
                "image_url": r.get("image_url")
            })
            ai_context.append({
                "title": r.get("title"),
                "color": r.get("color"),
                "size": r.get("size"),
                "price": r.get("price"),
                "type": r.get("type") or r.get("product_type")
            })

        return full_results, ai_context


ai_service = AIService()
