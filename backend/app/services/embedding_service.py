
import logging
from typing import List, Union, Tuple
from io import BytesIO
from PIL import Image
import httpx
import torch

from app.config.settings import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

class EmbeddingService:
    def __init__(self):
        # Image Model (Local SigLIP)
        self._siglip_model = None
        self._siglip_processor = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.siglip_model_id = "google/siglip-base-patch16-224"

    @property
    def siglip(self):
        """Lazy load SigLIP model to save RAM until needed."""
        if self._siglip_model is None:
            logger.info(f"Loading SigLIP model: {self.siglip_model_id} on {self.device}...")
            try:
                from transformers import AutoProcessor, AutoModel
                self._siglip_processor = AutoProcessor.from_pretrained(self.siglip_model_id)
                self._siglip_model = AutoModel.from_pretrained(self.siglip_model_id).to(self.device)
                logger.info("SigLIP model loaded successfully.")
            except Exception as e:
                logger.error(f"Failed to load SigLIP: {e}")
                raise e
        return self._siglip_model, self._siglip_processor

    def embed_text(self, text: Union[str, List[str]]) -> Union[List[float], List[List[float]]]:
        """
        Generate semantic text embeddings using OpenAI.
        Accepts a single string or a list of strings (batch mode).
        """
        try:
            from openai import OpenAI
            client = OpenAI(api_key=settings.openai_api_key)
            
            # Ensure input is a list for batch processing, but handle single string case
            is_single = isinstance(text, str)
            inputs = [text] if is_single else text
            
            # Create embeddings
            response = client.embeddings.create(input=inputs, model="text-embedding-3-large")
            
            embeddings = [data.embedding for data in response.data]
            return embeddings[0] if is_single else embeddings

        except Exception as e:
            logger.error(f"Error embedding text with OpenAI: {e}")
            raise e

    def embed_image(self, image_input: Union[str, Image.Image]) -> List[float]:
        """
        Generate visual embeddings using Google SigLIP (single image).
        """
        try:
            img = self._load_image(image_input)
            model, processor = self.siglip

            # Preprocess image
            inputs = processor(images=img, return_tensors="pt").to(self.device)

            # Run through SigLIP vision encoder
            with torch.no_grad():
                vision_output = model.vision_model(pixel_values=inputs["pixel_values"])

            # pooler_output is the CLS-token representation — shape: (1, 768)
            image_features = vision_output.pooler_output

            # Normalize (critical for cosine similarity)
            image_features = image_features / image_features.norm(p=2, dim=-1, keepdim=True)

            return image_features[0].cpu().tolist()

        except Exception as e:
            logger.error(f"Error embedding image with SigLIP: {e}")
            raise e

    def embed_images_batch(
        self,
        image_inputs: List[Union[str, Image.Image]],
        sub_batch_size: int = 16,
    ) -> List[Union[List[float], None]]:
        """
        Embed a list of images (URLs or PIL Images) using SigLIP.

        - Downloads all URLs concurrently (thread pool via httpx).
        - Runs SigLIP inference in sub-batches to fit GPU/CPU memory.
        - Returns a list of the same length as `image_inputs`.
          Failed items return None instead of raising.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        n = len(image_inputs)
        loaded: List[Union[Image.Image, None]] = [None] * n

        # ── 1. Download / load all images in parallel ─────────────────────────
        def load_one(idx_input):
            idx, inp = idx_input
            try:
                return idx, self._load_image(inp)
            except Exception as e:
                logger.warning(f"[img {idx}] Failed to load: {e}")
                return idx, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(load_one, (i, inp)): i for i, inp in enumerate(image_inputs)}
            for future in as_completed(futures):
                idx, img = future.result()
                loaded[idx] = img

        # ── 2. Run SigLIP on sub-batches ──────────────────────────────────────
        model, processor = self.siglip
        results: List[Union[List[float], None]] = [None] * n

        # Split into sub-batches, skipping indices that failed to load
        valid_indices = [i for i, img in enumerate(loaded) if img is not None]

        for start in range(0, len(valid_indices), sub_batch_size):
            batch_indices = valid_indices[start : start + sub_batch_size]
            batch_images  = [loaded[i] for i in batch_indices]

            try:
                inputs = processor(images=batch_images, return_tensors="pt").to(self.device)

                with torch.no_grad():
                    vision_output = model.vision_model(pixel_values=inputs["pixel_values"])

                # shape: (batch, 768)
                features = vision_output.pooler_output
                features = features / features.norm(p=2, dim=-1, keepdim=True)
                vectors  = features.cpu().tolist()

                for local_i, global_i in enumerate(batch_indices):
                    results[global_i] = vectors[local_i]

            except Exception as e:
                logger.error(f"SigLIP batch [{start}:{start+sub_batch_size}] failed: {e}")
                # individual items stay None

        return results

    def embed_query_for_image_search(self, text: str) -> List[float]:
        """
        Embed a text query using the SigLIP text encoder.
        The resulting vector lives in the same space as image embeddings,
        allowing text → image cross-modal search.
        """
        try:
            # Lazy load model if not already loaded
            if self._siglip_model is None:
                _, _ = self.siglip

            # Tokenize text (SigLIP uses max_length=64 for text)
            inputs = self._siglip_processor(
                text=[text], return_tensors="pt", padding="max_length"
            ).to(self.device)

            with torch.no_grad():
                text_output = self._siglip_model.text_model(
                    input_ids=inputs["input_ids"],
                    attention_mask=inputs.get("attention_mask"),
                )

            # pooler_output is the sentence representation — shape: (1, 768)
            text_features = text_output.pooler_output

            # Normalize
            text_features = text_features / text_features.norm(p=2, dim=-1, keepdim=True)

            return text_features[0].cpu().tolist()
        except Exception as e:
            logger.error(f"Error embedding query for image search: {e}")
            raise e

    def _load_image(self, image_input: Union[str, Image.Image]) -> Image.Image:
        """Helper to load image from URL, path, or object."""
        if isinstance(image_input, Image.Image):
            return image_input.convert("RGB")
        
        if isinstance(image_input, str):
            if image_input.startswith("http://") or image_input.startswith("https://"):
                try:
                    response = httpx.get(image_input, timeout=15)
                    response.raise_for_status()
                    return Image.open(BytesIO(response.content)).convert("RGB")
                except Exception as e:
                    logger.error(f"Failed to fetch image from URL {image_input}: {e}")
                    raise e
            else:
                try:
                    return Image.open(image_input).convert("RGB")
                except Exception as e:
                    logger.error(f"Failed to load image from path {image_input}: {e}")
                    raise e
        
        raise ValueError("Unsupported image input type")

# Singleton instance
embedding_service = EmbeddingService()
