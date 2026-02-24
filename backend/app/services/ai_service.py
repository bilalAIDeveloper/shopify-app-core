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
        phone_number: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Main entry point for handling an incoming WhatsApp message with AI.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

        # Construct the user message
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

        search_results = []

        try:
            logger.info(f"Sending message to OpenAI for user {phone_number}")
            
            # Initial call to OpenAI
            response = await self.client.chat.completions.create(
                model="gpt-4o-mini", # Using 4o-mini as it supports vision and function calling efficiently
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
                        
                        logger.info(f"AI requested search with query: '{search_query}', color: {color_filter}, max_price: {max_price}")
                        
                        # Execute the search
                        search_results = await self._execute_search(
                            text_query=search_query,
                            original_media_url=media_url, # Pass the original media for hybrid vector search
                            color=color_filter,
                            max_price=max_price
                        )
                        
                        # Append the tool's result to the messages
                        messages.append({
                            "tool_call_id": tool_call.id,
                            "role": "tool",
                            "name": "search_products",
                            "content": json.dumps(search_results)
                        })

                # Second call to OpenAI to generate the final response using the tool results
                logger.info("Sending tool results back to OpenAI")
                final_response = await self.client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    max_tokens=500
                )
                
                return {
                    "text": final_response.choices[0].message.content,
                    "products": search_results
                }

            else:
                # The model didn't call the tool, just answered directly
                return {
                    "text": response_message.content,
                    "products": []
                }

        except Exception as e:
            logger.error(f"Error processing message with AI: {e}")
            return {
                "text": "I'm sorry, I'm having trouble connecting to my system right now. Please try again later.",
                "products": []
            }

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

        # Generate Text Vector
        if text_query:
            try:
                text_vector = embedding_service.embed_text(text_query)
            except Exception as e:
                logger.error(f"Failed to embed text query: {e}")

        # Generate Image Vector (If media was provided)
        if original_media_url:
            try:
                # We can download the image and embed it using SigLIP
                logger.info(f"Embedding image from WhatsApp: {original_media_url}")
                # Note: this blocks the async event loop in httpx inside embedding_service, 
                # but it's acceptable for now. To make it purely async, embedding_service should be updated.
                image_vector = embedding_service.embed_image(original_media_url)
            except Exception as e:
                logger.error(f"Failed to embed image from WA: {e}")

        # Perform Hybrid Search search_service.perform_hybrid_search
        results = search_service.perform_hybrid_search(
            query=text_query,
            text_vector=text_vector,
            image_vector=image_vector,
            limit=3,  # Top 3 deals 
            filter_str=filter_str
        )

        # Simplify results to save tokens for the LLM
        simplified_results = []
        for r in results:
            simplified_results.append({
                "title": r.get("title"),
                "color": r.get("color"),
                "size": r.get("size"),
                "price": r.get("price"),
                "handle": r.get("handle"),
                "image_url": r.get("image_url")
            })

        return simplified_results


ai_service = AIService()
