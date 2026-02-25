"""
test_search.py
──────────────
Two test modes in one script:

  MODE 1 – RAW SEARCH (positional query arg)
  Tests Meilisearch directly, bypassing OpenAI entirely. Fast & deterministic.

      python test_search.py "black cargo pants"
      python test_search.py "blue jeans" --color BLUE --max-price 1500
      python test_search.py "navy t-shirt" --text-only

  MODE 2 – FULL AI PIPELINE (--ai flag)
  Sends the message through the full GPT prompt → tool call → search → response
  flow and prints a structured breakdown of every step, including:
    • What arguments GPT sent to search_products
    • The resulting filter string and vector shapes
    • The raw Meilisearch hits
    • The final AI response

      python test_search.py --ai "blue jeans for boys"
      python test_search.py --ai "shirts under 1000"
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── ANSI colours ────────────────────────────────────────────────────────────
CYAN    = "\033[96m"
YELLOW  = "\033[93m"
GREEN   = "\033[92m"
RED     = "\033[91m"
BOLD    = "\033[1m"
RESET   = "\033[0m"

def banner(title: str):
    bar = "━" * 66
    print(f"\n{CYAN}{BOLD}{bar}\n  {title}\n{bar}{RESET}")

def section(title: str):
    print(f"\n{YELLOW}{BOLD}▶ {title}{RESET}")
    print(f"  {'─' * 60}")

def kv(key: str, value):
    colour = RED if value in (None, "None", "(not set)") else ""
    print(f"  {BOLD}{key:<18}{RESET}: {colour}{value}{RESET}")


# ═══════════════════════════════════════════════════════════════════════════════
# MODE 1 — Direct Meilisearch search (no GPT)
# ═══════════════════════════════════════════════════════════════════════════════

def build_filter(color: Optional[str], max_price: Optional[float]) -> Optional[str]:
    parts = []
    if color:
        parts.append(f'color = "{color.upper()}"')
    if max_price is not None:
        parts.append(f"price <= {max_price}")
    return " AND ".join(parts) if parts else None


def search_with_embedder(index, query, vector, embedder, ratio, limit, filter_str):
    params: Dict[str, Any] = {
        "hybrid": {"embedder": embedder, "semanticRatio": ratio},
        "vector": vector,
        "limit": limit,
        "attributesToRetrieve": ["id", "title", "type", "color", "size",
                                  "price", "image_url", "handle", "search_text"],
    }
    if filter_str:
        params["filter"] = filter_str

    print(f"\n  {BOLD}[{embedder}] Meilisearch params:{RESET}")
    safe = {k: (v if k != "vector" else f"<{len(v)}-dim vector>") for k, v in params.items()}
    for k, v in safe.items():
        print(f"    {k}: {v}")

    result = index.search(query, params)
    return result.get("hits", [])


def merge_results(text_hits, image_hits):
    seen: Dict[str, Dict] = {}
    for hit in text_hits:
        pid = hit["id"]
        hit["_sources"] = ["text"]
        hit["_score"]   = 1
        seen[pid] = hit
    for hit in image_hits:
        pid = hit["id"]
        if pid in seen:
            seen[pid]["_score"]   = 2
            seen[pid]["_sources"].append("image")
        else:
            hit["_sources"] = ["image"]
            hit["_score"]   = 1
            seen[pid] = hit
    return sorted(seen.values(), key=lambda h: h["_score"], reverse=True)


def print_hits(hits, query):
    section(f"RESULTS  (query: \"{query}\", {len(hits)} unique hits)")
    if not hits:
        print(f"  {RED}No results returned.{RESET}")
        return
    for i, h in enumerate(hits, 1):
        stars   = "⭐⭐" if h["_score"] == 2 else "⭐ "
        sources = " + ".join(h["_sources"])
        price   = f"PKR {h.get('price', '?'):,.0f}" if h.get("price") else "?"
        print(f"\n  [{i}] {stars} {BOLD}{h.get('title', 'N/A')}{RESET}")
        print(f"       Color   : {h.get('color', '?')}  |  Size: {h.get('size', '?')}  |  Price: {price}")
        print(f"       Match   : {sources}")
        if h.get("image_url"):
            print(f"       Image   : {h['image_url']}")


def run_direct(args):
    from app.config.settings import settings
    from app.services.embedding_service import embedding_service
    import meilisearch

    query      = args.query
    filter_str = build_filter(args.color, args.max_price)

    banner(f"MODE 1 — DIRECT SEARCH: \"{query}\"")

    kv("query",       query)
    kv("filter_str",  filter_str or "(none)")
    kv("limit",       args.limit)
    kv("ratio",       args.ratio)

    client = meilisearch.Client(settings.meilisearch_url, settings.meilisearch_master_key)
    try:
        client.health()
    except Exception as e:
        print(f"{RED}❌  Cannot reach Meilisearch: {e}{RESET}")
        sys.exit(1)

    index = client.get_index(settings.meilisearch_index)

    section("EMBEDDING QUERY")
    text_vector  = None
    image_vector = None

    if not args.image_only:
        print("  OpenAI text embedding…", end=" ", flush=True)
        try:
            text_vector = embedding_service.embed_text(query)
            print(f"{GREEN}✓  ({len(text_vector)}-dim){RESET}")
        except Exception as e:
            print(f"{RED}❌  {e}{RESET}")

    if not args.text_only:
        print("  SigLIP image embedding…", end=" ", flush=True)
        try:
            image_vector = embedding_service.embed_query_for_image_search(query)
            print(f"{GREEN}✓  ({len(image_vector)}-dim){RESET}")
        except Exception as e:
            print(f"{RED}❌  {e}{RESET}")

    section("MEILISEARCH QUERIES")
    text_hits  = []
    image_hits = []
    if text_vector:
        text_hits  = search_with_embedder(index, query, text_vector,  "text",  args.ratio, args.limit, filter_str)
    if image_vector:
        image_hits = search_with_embedder(index, query, image_vector, "image", args.ratio, args.limit, filter_str)

    merged = merge_results(text_hits, image_hits)
    print_hits(merged, query)

    print(f"\n  text hits: {len(text_hits)}  |  image hits: {len(image_hits)}  |  double-matched: {sum(1 for h in merged if h['_score'] == 2)}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# MODE 2 — Full AI pipeline (GPT prompt → tool call → search)
# ═══════════════════════════════════════════════════════════════════════════════

async def run_one_turn(
    ai_service,
    user_input: str,
    chat_history: List[Dict[str, str]],
    captured: Dict[str, Any],
    patched_create,
    patched_execute,
):
    """Process a single turn and append to chat_history."""
    import types
    captured.clear()
    ai_service.client.chat.completions.create = patched_create
    ai_service._execute_search = types.MethodType(patched_execute, ai_service)

    final_response = await ai_service.process_whatsapp_message(
        text_content=user_input,
        chat_history=chat_history,
    )

    # ── Display hits ─────────────────────────────────────────────────────────
    hits = captured.get("full", [])
    if hits:
        section(f"RAW SEARCH HITS  ({len(hits)} returned)")
        col = [20, 10, 14, 8, 9, 5, 7]
        hdrs = ["Title", "Color", "Handle", "Size", "Price", "Hit", "Rank"]
        print("  " + "  ".join(f"{h:<{w}}" for h, w in zip(hdrs, col)))
        print("  " + "─" * (sum(col) + len(col) * 2))
        for h in hits:
            size_raw = h.get("size", "") or ""
            if isinstance(size_raw, list):
                size_raw = ", ".join(size_raw)
            row = [
                str(h.get("title",  ""))[:col[0]],
                str(h.get("color",  ""))[:col[1]],
                str(h.get("handle", ""))[:col[2]],
                str(size_raw)[:col[3]],
                str(h.get("price",  ""))[:col[4]],
                str(h.get("_score", ""))[:col[5]],
                str(h.get("_rankingScore", ""))[:col[6]],
            ]
            print("  " + "  ".join(f"{v:<{w}}" for v, w in zip(row, col)))

    section("AI RESPONSE")
    print(f"\n  {GREEN}Isla: {final_response}{RESET}\n")

    # Append both sides to history for the next turn
    chat_history.append({"role": "user",      "content": user_input})
    chat_history.append({"role": "assistant",  "content": final_response or ""})

    return final_response


async def run_ai_pipeline(initial_query: Optional[str] = None):
    from app.services.ai_service import ai_service

    banner("MODE 2 — FULL AI PIPELINE  (type 'quit' to exit)")

    chat_history: List[Dict[str, str]] = []
    captured: Dict[str, Any] = {}
    original_create  = ai_service.client.chat.completions.create
    original_execute = ai_service._execute_search.__func__

    # ── Interceptors (defined once, reused every turn) ────────────────────────
    async def patched_create(**kwargs):
        resp = await original_create(**kwargs)
        msg  = resp.choices[0].message
        if msg.tool_calls:
            section("GPT TOOL CALL  →  search_products")
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                captured["tool_args"] = args
                kv("search_query",  args.get("search_query"))
                kv("color_filter",  args.get("color_filter") or "(not set)")
                kv("max_price",     args.get("max_price") or "(not set)")
                kv("searching_msg", args.get("searching_message", ""))
        return resp

    async def patched_execute(self, text_query, original_media_url=None, color=None, max_price=None):
        filters = []
        if color:
            filters.append(f'color = "{color.upper()}"')
        if max_price is not None:
            filters.append(f"price <= {max_price}")
        filter_str = " AND ".join(filters) if filters else None

        section("SEARCH EXECUTION PAYLOAD")
        kv("text_query",  text_query)
        kv("color",       color or "(not set)")
        kv("max_price",   max_price or "(not set)")
        kv("filter_str",  filter_str or "(none)")
        kv("media_url",   original_media_url or "(none)")

        full, ai_ctx, search_context = await original_execute(self, text_query, original_media_url, color, max_price)
        captured["full"]   = full
        captured["ai_ctx"] = ai_ctx
        captured["search_context"] = search_context
        return full, ai_ctx, search_context

    # ── Conversation loop ─────────────────────────────────────────────────────
    turn = 0
    user_input = initial_query

    while True:
        turn += 1
        if user_input is None:
            try:
                print(f"\n{BOLD}You:{RESET} ", end="", flush=True)
                user_input = input().strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye! 👋")
                break

        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye! 👋")
            break
        if not user_input:
            user_input = None
            continue

        print(f"\n{'═' * 66}")
        print(f"  Turn {turn}  |  User: {BOLD}{user_input}{RESET}")
        print(f"{'═' * 66}")

        await run_one_turn(
            ai_service, user_input, chat_history,
            captured, patched_create, patched_execute,
        )

        user_input = None   # reset so next iteration reads from stdin


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Search pipeline test — direct Meilisearch (default) or full AI (--ai)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("query",          type=str,   nargs="?",   help="Optional opening query")
    parser.add_argument("--ai",           action="store_true",     help="Run full AI pipeline (GPT + tool call, conversation loop)")
    parser.add_argument("--limit",        type=int,   default=6)
    parser.add_argument("--ratio",        type=float, default=0.6, help="Semantic ratio 0–1 (default 0.6)")
    parser.add_argument("--color",        type=str,   default=None)
    parser.add_argument("--max-price",    type=float, default=None)
    parser.add_argument("--text-only",    action="store_true")
    parser.add_argument("--image-only",   action="store_true")
    args = parser.parse_args()

    if args.ai:
        # In AI mode the first query is optional — the loop will prompt if missing
        asyncio.run(run_ai_pipeline(initial_query=args.query))
    else:
        if not args.query:
            print(f"\n{BOLD}Enter a search query (direct Meilisearch):{RESET}")
            args.query = input("  > ").strip()
            if not args.query:
                print("No query provided. Exiting.")
                sys.exit(0)
        run_direct(args)


if __name__ == "__main__":
    main()
