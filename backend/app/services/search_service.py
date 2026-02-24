
import meilisearch
from typing import List, Dict, Any, Optional
from app.config.settings import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

class SearchService:
    def __init__(self):
        self._client = None
        self.url = settings.meilisearch_url
        self.api_key = settings.meilisearch_master_key

    @property
    def client(self) -> meilisearch.Client:
        if not self._client:
            try:
                self._client = meilisearch.Client(
                    self.url,
                    self.api_key
                )
                # Verify connection
                self._client.health() 
                logger.info(f"Connected to Meilisearch at {self.url}")
            except Exception as e:
                logger.error(f"Failed to connect to Meilisearch: {e}")
                # We might not want to crash the app, just log error
        return self._client

    def get_index(self, index_name: str):
        """
        Get or create an index.
        """
        if not self.client:
            logger.warning("Meilisearch client not initialized.")
            return None
            
        try:
            # Check if index exists, create if not
            try:
                index = self.client.get_index(index_name)
            except meilisearch.errors.MeilisearchApiError:
               task = self.client.create_index(index_name, {'primaryKey': 'id'})
               self.client.wait_for_task(task['taskUid'])
               index = self.client.get_index(index_name)
            return index
        except Exception as e:
            logger.error(f"Error getting index {index_name}: {e}")
            return None

    def update_settings(self, index_name: str, settings_dict: Dict[str, Any]):
        """
        Update index settings (e.g. for vector search).
        """
        index = self.get_index(index_name)
        if not index:
            return
        
        try:
            task = index.update_settings(settings_dict)
            logger.info(f"Updated settings for index {index_name}: {task} (taskUid: {task['taskUid']})")
            return task
        except Exception as e:
            logger.error(f"Error updating settings for {index_name}: {e}")

    def add_documents(self, index_name: str, documents: List[Dict[str, Any]]):
        """
        Add documents to index.
        """
        index = self.get_index(index_name)
        if not index:
            return

        try:
            task = index.add_documents(documents)
            logger.info(f"Added {len(documents)} documents to {index_name}: {task}")
            return task
        except Exception as e:
            logger.error(f"Error adding documents to {index_name}: {e}")

    def search(self, index_name: str, query: str = "", vector: Optional[List[float]] = None, limit: int = 10, filter: str = None) -> Dict[str, Any]:
        """
        Search in index. Supports full-text and vector search.
        """
        index = self.get_index(index_name)
        if not index:
            return {}

        try:
            search_params = {
                'limit': limit,
            }
            if vector:
                search_params['vector'] = vector
            if filter:
                search_params['filter'] = filter
            
            # If query is empty but vector is provided, Meilisearch handles it if vector search is enabled.
            # Usually vector search requires `vector` param and can be combined with `q` (hybrid) or without (pure vector).
            
            result = index.search(query, search_params)
            return result
        except Exception as e:
            logger.error(f"Error searching {index_name}: {e}")
            return {}

    def perform_hybrid_search(
        self,
        query: str,
        text_vector: Optional[List[float]] = None,
        image_vector: Optional[List[float]] = None,
        limit: int = 10,
        semantic_ratio: float = 0.6,
        filter_str: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Runs dual hybrid search (text embedder vs image embedder) and merges results.
        """
        index = self.get_index(settings.meilisearch_index)
        if not index:
            return []

        text_hits = []
        image_hits = []

        # 1. Search via Text Embedder (OpenAI)
        if text_vector:
            params = {
                "hybrid": {"embedder": "text", "semanticRatio": semantic_ratio},
                "vector": text_vector,
                "limit": limit
            }
            if filter_str:
                params["filter"] = filter_str
            res = index.search(query, params)
            text_hits = res.get("hits", [])

        # 2. Search via Image Embedder (SigLIP)
        if image_vector:
            params = {
                "hybrid": {"embedder": "image", "semanticRatio": semantic_ratio},
                "vector": image_vector,
                "limit": limit
            }
            if filter_str:
                params["filter"] = filter_str
            res = index.search(query, params)
            image_hits = res.get("hits", [])

        # 3. Merge & Score (Double Hit Heuristic)
        seen: Dict[str, Dict] = {}

        for hit in text_hits:
            pid = hit["id"]
            hit["_sources"] = ["text"]
            hit["_score"] = 1
            seen[pid] = hit

        for hit in image_hits:
            pid = hit["id"]
            if pid in seen:
                seen[pid]["_score"] = 2
                seen[pid]["_sources"].append("image")
            else:
                hit["_sources"] = ["image"]
                hit["_score"] = 1
                seen[pid] = hit

        return sorted(seen.values(), key=lambda h: h["_score"], reverse=True)

# Singleton instance
search_service = SearchService()
