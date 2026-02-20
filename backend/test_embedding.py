"""
Quick test for the EmbeddingService.

Usage:
    # Test image via URL:
    python test_embedding.py --image https://example.com/shoe.jpg

    # Test image via local path:
    python test_embedding.py --image path/to/image.jpg

    # Test text:
    python test_embedding.py --text "red running shoes"

    # Test text-to-image query (SigLIP text encoder):
    python test_embedding.py --query "shiny red high heels"

    # Test similarity between a query and an image:
    python test_embedding.py --image https://example.com/shoe.jpg --query "red shoes"
"""

import argparse
import sys
import os

# Make sure 'backend' dir is on the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.embedding_service import embedding_service


def cosine_similarity(vec_a, vec_b) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = sum(a ** 2 for a in vec_a) ** 0.5
    norm_b = sum(b ** 2 for b in vec_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def main():
    parser = argparse.ArgumentParser(description="Test EmbeddingService")
    parser.add_argument("--image", type=str, help="Image URL or local path to embed")
    parser.add_argument("--text",  type=str, help="Text to embed using OpenAI")
    parser.add_argument("--query", type=str, help="Text query to embed using SigLIP (for image search)")
    args = parser.parse_args()

    if not any([args.image, args.text, args.query]):
        parser.print_help()
        sys.exit(1)

    image_vector = None
    query_vector = None

    # ── Image Embedding ───────────────────────────────────────────────────────
    if args.image:
        print(f"\n{'─'*60}")
        print(f"[IMAGE] Embedding: {args.image}")
        try:
            image_vector = embedding_service.embed_image(args.image)
            print(f"  ✅ Success! Vector dimension: {len(image_vector)}")
            print(f"  First 5 values: {[round(v, 4) for v in image_vector[:5]]}")
        except Exception as e:
            print(f"  ❌ Error: {e}")

    # ── Text Embedding (OpenAI) ───────────────────────────────────────────────
    if args.text:
        print(f"\n{'─'*60}")
        print(f"[TEXT/OpenAI] Embedding: '{args.text}'")
        try:
            text_vector = embedding_service.embed_text(args.text)
            print(f"  ✅ Success! Vector dimension: {len(text_vector)}")
            print(f"  First 5 values: {[round(v, 4) for v in text_vector[:5]]}")
        except Exception as e:
            print(f"  ❌ Error: {e}")

    # ── Query Embedding (SigLIP for image search) ─────────────────────────────
    if args.query:
        print(f"\n{'─'*60}")
        print(f"[QUERY/SigLIP] Embedding: '{args.query}'")
        try:
            query_vector = embedding_service.embed_query_for_image_search(args.query)
            print(f"  ✅ Success! Vector dimension: {len(query_vector)}")
            print(f"  First 5 values: {[round(v, 4) for v in query_vector[:5]]}")
        except Exception as e:
            print(f"  ❌ Error: {e}")
            query_vector = None

    # ── Similarity Check (if both image and query provided) ───────────────────
    if image_vector and query_vector:
        print(f"\n{'─'*60}")
        print("[SIMILARITY] Text query vs. Image")
        score = cosine_similarity(image_vector, query_vector)
        print(f"  Cosine Similarity: {score:.4f}  (range -1.0 to 1.0, higher = more similar)")
        if score > 0.25:
            print("  ✅ Good match — the query likely describes this image!")
        else:
            print("  ⚠️  Low match — the query may not describe this image.")
    
    print(f"\n{'─'*60}\n")


if __name__ == "__main__":
    main()
