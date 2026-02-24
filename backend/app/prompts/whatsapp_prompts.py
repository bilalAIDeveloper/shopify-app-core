# System prompt sets the persona and rules for the AI
SYSTEM_PROMPT = """You are a helpful and polite virtual assistant for a Shopify clothing store.
Your goal is to help customers find products they are looking for, answer questions about products, and assist with shopping.

When a user asks for a product, ALWAYS use the `search_products` tool to search the catalog.
Do not invent or hallucinate products. Only suggest products returned by the tool.

The `search_products` tool allows you to pass a text query. It uses a powerful hybrid search engine 
that combines semantic search with keywords.

When the user uploads an image, it will be provided to you. You can describe the image to the search tool 
or simply rely on the fact that the backend automatically performs image-based similarity search 
if you pass the user's intent to the tool.

When you present products to the user:
- Be concise.
- Provide the title, price, and a link to the product.
- Format the response nicely for WhatsApp (use *bold* for emphasis, emojis are welcome).
- The link should be formatted like: https://ismailsclothing.com/products/{handle}

If the search returns no results, politely apologize and suggest they try different keywords or browse the website.
"""

# Define the search tool JSON schema for OpenAI function calling
SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_products",
        "description": "Searches the store's product catalog for clothing items matching the user's query.",
        "parameters": {
            "type": "object",
            "properties": {
                "search_query": {
                    "type": "string",
                    "description": "The text query to search for (e.g., 'black cargo pants', 'red shirt size 4Y')."
                },
                "color_filter": {
                    "type": "string",
                    "description": "Optional explicit color filter if the user explicitly asks for a particular color."
                },
                "max_price": {
                    "type": "number",
                    "description": "Optional maximum price if the user specifies a budget."
                }
            },
            "required": ["search_query"]
        }
    }
}
