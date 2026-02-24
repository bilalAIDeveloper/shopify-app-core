"""
test_search.py
──────────────
Interactive hybrid search test against the Meilisearch products index.

Runs two searches in parallel:
  1. Hybrid on the "text" embedder  (OpenAI 3072-dim + BM25 on search_text)
  2. Hybrid on the "image" embedder (SigLIP text encoder 768-dim + BM25)

Then merges and de-dupes by product, boosting items that appeared in both.

Usage:
    python test_search.py "black cargo pants"
    python test_search.py "red shirt for boys" --limit 5
    python test_search.py "blue jeans" --ratio 0.8   # more vector, less BM25
    python test_search.py "navy t-shirt" --color NAVY
    python test_search.py "jeans under 1000" --max-price 1000
"""

import argparse
import sys
import os
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config.settings import settings
from app.services.embedding_service import embedding_service
import meilisearch

INDEX_NAME = settings.meilisearch_index


# ──────────────────────────────────────────────────────────────────────────────

def build_filter(color: Optional[str], max_price: Optional[float]) -> Optional[str]:
    parts = []
    if color:
        parts.append(f'color = "{color.upper()}"')
    if max_price is not None:
        parts.append(f"price <= {max_price}")
    return " AND ".join(parts) if parts else None


def search_with_embedder(
    index,
    query: str,
    vector: List[float],
    embedder: str,
    ratio: float,
    limit: int,
    filter_str: Optional[str],
) -> List[Dict[str, Any]]:
    """Run a single hybrid search using a named embedder."""
    params: Dict[str, Any] = {
        "hybrid": {
            "embedder":     embedder,
            "semanticRatio": ratio,   # 0.0 = pure BM25, 1.0 = pure vector
        },
        "vector": vector,
        "limit":  limit,
        "attributesToRetrieve": [
            "id", "title", "type", "color", "size",
            "price", "image_url", "handle", "search_text",
        ],
    }
    if filter_str:
        params["filter"] = filter_str

    result = index.search(query, params)
    return result.get("hits", [])


def merge_results(
    text_hits: List[Dict],
    image_hits: List[Dict],
) -> List[Dict]:
    """
    Merge hits from both embedders.
    Products appearing in both get a relevance bonus (score = 2).
    Results are sorted by score descending.
    """
    seen: Dict[str, Dict] = {}

    for hit in text_hits:
        pid = hit["id"]
        hit["_sources"] = ["text"]
        hit["_score"]   = 1
        seen[pid] = hit

    for hit in image_hits:
        pid = hit["id"]
        if pid in seen:
            seen[pid]["_score"]   = 2          # double hit → higher relevance
            seen[pid]["_sources"].append("image")
        else:
            hit["_sources"] = ["image"]
            hit["_score"]   = 1
            seen[pid] = hit

    return sorted(seen.values(), key=lambda h: h["_score"], reverse=True)


def print_results(results: List[Dict], query: str):
    sep = "─" * 64
    print(f"\n{sep}")
    print(f"  Query  : \"{query}\"")
    print(f"  Found  : {len(results)} unique products")
    print(sep)

    if not results:
        print("  No results found.\n")
        return

    for i, hit in enumerate(results, 1):
        score_badge = "⭐⭐" if hit["_score"] == 2 else "⭐ "
        sources     = " + ".join(hit["_sources"])
        price       = f"PKR {hit.get('price', '?'):,.0f}" if hit.get("price") else "?"
        url         = f"https://ismailsclothing.com/products/{hit.get('handle', '')}"

        print(f"\n  [{i}] {score_badge} {hit.get('title', 'N/A')}")
        print(f"       Color   : {hit.get('color', '?')}  |  Size: {hit.get('size', '?')}  |  Price: {price}")
        print(f"       Type    : {hit.get('type', '?')}")
        print(f"       Match   : {sources}")
        print(f"       URL     : {url}")
        if hit.get("image_url"):
            print(f"       Image   : {hit['image_url']}")

    print(f"\n{sep}\n")


# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hybrid search test for products index")
    parser.add_argument("query",          type=str,   help="Search query")
    parser.add_argument("--limit",        type=int,   default=6,    help="Max results per embedder (default: 6)")
    parser.add_argument("--ratio",        type=float, default=0.6,  help="Semantic ratio 0.0–1.0 (default: 0.6)")
    parser.add_argument("--color",        type=str,   default=None, help="Hard filter by color (e.g. BLACK)")
    parser.add_argument("--max-price",    type=float, default=None, help="Hard filter: max price")
    parser.add_argument("--text-only",    action="store_true",      help="Only use text embedder")
    parser.add_argument("--image-only",   action="store_true",      help="Only use image embedder")
    args = parser.parse_args()

    query      = args.query
    filter_str = build_filter(args.color, args.max_price)

    # ── Connect ───────────────────────────────────────────────────────────────
    client = meilisearch.Client(settings.meilisearch_url, settings.meilisearch_master_key)
    try:
        client.health()
    except Exception as e:
        print(f"❌  Cannot reach Meilisearch: {e}")
        sys.exit(1)

    index = client.get_index(INDEX_NAME)

    print(f"\nEmbedding query: \"{query}\"")
    if filter_str:
        print(f"Hard filters   : {filter_str}")

    # ── Embed query ───────────────────────────────────────────────────────────
    text_vector  = None
    image_vector = None

    if not args.image_only:
        print("  → OpenAI (text embedder)…", end=" ", flush=True)
        try:
            text_vector = embedding_service.embed_text(query)
            print(f"✓  ({len(text_vector)}-dim)")
        except Exception as e:
            print(f"❌  {e}")

    if not args.text_only:
        print("  → SigLIP  (image embedder)…", end=" ", flush=True)
        try:
            image_vector = embedding_service.embed_query_for_image_search(query)
            print(f"✓  ({len(image_vector)}-dim)")
        except Exception as e:
            print(f"❌  {e}")

    # ── Search ────────────────────────────────────────────────────────────────
    text_hits  = []
    image_hits = []

    if text_vector:
        text_hits = search_with_embedder(
            index, query, text_vector, "text",
            args.ratio, args.limit, filter_str,
        )

    if image_vector:
        image_hits = search_with_embedder(
            index, query, image_vector, "image",
            args.ratio, args.limit, filter_str,
        )

    # ── Merge & display ───────────────────────────────────────────────────────
    merged = merge_results(text_hits, image_hits)
    print_results(merged, query)

    # ── Raw stats ─────────────────────────────────────────────────────────────
    print(f"  text embedder hits  : {len(text_hits)}")
    print(f"  image embedder hits : {len(image_hits)}")
    double = sum(1 for h in merged if h["_score"] == 2)
    print(f"  double-matched      : {double}  (appeared in both → highest relevance)\n")


if __name__ == "__main__":
    main()
