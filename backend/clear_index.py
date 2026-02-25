"""
clear_index.py
──────────────
Deletes all documents (and their associated vectors) from the Meilisearch
products index.

Usage:
    python clear_index.py              # delete all documents, keep index + settings
    python clear_index.py --drop       # drop the entire index (settings lost too)
    python clear_index.py --dry-run    # show what would be deleted without doing it
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import meilisearch
from app.config.settings import settings

# ── ANSI colours ─────────────────────────────────────────────────────────────
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def connect() -> meilisearch.Client:
    client = meilisearch.Client(settings.meilisearch_url, settings.meilisearch_master_key)
    try:
        client.health()
        print(f"{GREEN}✓  Connected to Meilisearch at {settings.meilisearch_url}{RESET}")
    except Exception as e:
        print(f"{RED}❌  Cannot reach Meilisearch: {e}{RESET}")
        sys.exit(1)
    return client


def get_doc_count(index) -> int:
    try:
        return index.get_stats().number_of_documents
    except Exception:
        return 0


def wait_for_task(client: meilisearch.Client, task_uid: int, description: str):
    """Poll until the task completes."""
    import time
    print(f"   Waiting for task {task_uid} ({description})…", end=" ", flush=True)
    while True:
        task = client.get_task(task_uid)
        status = task.status
        if status == "succeeded":
            print(f"{GREEN}done{RESET}")
            return
        elif status == "failed":
            print(f"{RED}FAILED — {task.error}{RESET}")
            sys.exit(1)
        time.sleep(0.5)
        print(".", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Clear Meilisearch product index vectors/documents")
    parser.add_argument("--drop",    action="store_true", help="Drop the entire index (loses settings & embedder config)")
    parser.add_argument("--dry-run", action="store_true", help="Show counts without deleting anything")
    args = parser.parse_args()

    index_name = settings.meilisearch_index
    client = connect()

    # ── Check index exists ────────────────────────────────────────────────────
    try:
        index = client.get_index(index_name)
    except Exception:
        print(f"{YELLOW}⚠  Index '{index_name}' does not exist. Nothing to clear.{RESET}")
        sys.exit(0)

    doc_count = get_doc_count(index)

    print(f"\n  Index      : {BOLD}{index_name}{RESET}")
    print(f"  Documents  : {BOLD}{doc_count}{RESET}  (each document carries text + image vectors)")

    if args.dry_run:
        print(f"\n{YELLOW}[dry-run] No changes made.{RESET}")
        sys.exit(0)

    # ── Confirm ───────────────────────────────────────────────────────────────
    if args.drop:
        action = f"DROP the entire index '{index_name}' (settings will be lost)"
    else:
        action = f"DELETE all {doc_count} documents from '{index_name}' (index settings kept)"

    print(f"\n{RED}{BOLD}⚠  About to: {action}{RESET}")
    confirm = input("  Type 'yes' to confirm: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        sys.exit(0)

    # ── Execute ───────────────────────────────────────────────────────────────
    if args.drop:
        task = client.delete_index(index_name)
        wait_for_task(client, task.task_uid, "delete index")
        print(f"\n{GREEN}✓  Index '{index_name}' dropped.{RESET}")
        print(f"   Run {BOLD}python ingest_from_json.py{RESET} to recreate it with fresh settings.\n")
    else:
        task = index.delete_all_documents()
        wait_for_task(client, task.task_uid, "delete all documents")

        remaining = get_doc_count(index)
        print(f"\n{GREEN}✓  All documents (and their vectors) deleted.{RESET}")
        print(f"   Documents remaining : {remaining}")
        print(f"   Index settings kept : embedders, filterable attributes, etc.")
        print(f"   Run {BOLD}python ingest_from_json.py{RESET} to re-ingest.\n")


if __name__ == "__main__":
    main()
