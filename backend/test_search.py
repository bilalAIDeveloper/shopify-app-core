"""
test_search.py
──────────────
Full AI pipeline:
Sends the message through the full GPT prompt → tool call → search → response
flow and prints a structured breakdown of every step, including:
  • What arguments GPT sent to search_products
  • The resulting filter string and vector shapes
  • The raw Meilisearch hits
  • The final AI response

    python test_search.py "blue jeans for boys"
    python test_search.py "shirts under 1000"
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import types
from typing import Any, Dict, List, Optional

from app.database.engine import Base, engine
from app.database.models import ShopInstallation
from app.database.models.product_session import ProductSession
from app.services.ai_service import ai_service

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Enable basic logging to see internal loggers (ai_service, search_service, etc.)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

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
# Full AI pipeline (GPT prompt → tool call → search)
# ═══════════════════════════════════════════════════════════════════════════════

async def run_one_turn(
    ai_service,
    user_input: str,
    chat_history: List[Dict[str, str]],
    captured: Dict[str, Any],
    patched_create,
    patched_execute,
    phone_number: Optional[str] = None,
):
    """Process a single turn and append to chat_history."""
    captured.clear()
    ai_service.client.chat.completions.create = patched_create
    ai_service._execute_search = types.MethodType(patched_execute, ai_service)

    final_response = await ai_service.process_whatsapp_message(
        text_content=user_input,
        chat_history=chat_history,
        phone_number=phone_number,
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


async def run_ai_pipeline(
    initial_query: Optional[str] = None,
    phone_number: Optional[str] = None,
):
    phone_display = f"  Phone : {BOLD}{phone_number}{RESET}\n" if phone_number else ""
    banner(f"FULL AI PIPELINE  (type 'quit' to exit)\n{phone_display}")

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

    async def patched_execute(self, text_query, original_media_url=None, color=None, max_price=None, exclude_handles=None):
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
        if exclude_handles:
            kv("exclude_h",   str(exclude_handles))

        full, ai_ctx, search_context = await original_execute(
            self, text_query, original_media_url, color, max_price, exclude_handles
        )
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
            phone_number=phone_number,
        )

        user_input = None   # reset so next iteration reads from stdin


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    Base.metadata.create_all(bind=engine)

    parser = argparse.ArgumentParser(
        description="Search pipeline test — full AI conversation loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("query",          type=str,   nargs="?",   help="Optional opening query")
    parser.add_argument("--phone",        type=str,   default=None, help="Phone number to simulate (E.164 without +, e.g. 923001234567)")
    args = parser.parse_args()

    asyncio.run(run_ai_pipeline(initial_query=args.query, phone_number=args.phone))


if __name__ == "__main__":
    main()
