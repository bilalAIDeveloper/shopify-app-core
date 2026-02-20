
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
        Used for product descriptions and text-to-text search.
        """
        try:
            from openai import OpenAI
            client = OpenAI(api_key=settings.openai_api_key)
            
            # Ensure input is a list for batch processing, but handle single string case
            is_single = isinstance(text, str)
            inputs = [text] if is_single else text
            
            # Create embeddings
            response = client.embeddings.create(input=inputs, model="text-embedding-3-small")
            
            embeddings = [data.embedding for data in response.data]
            return embeddings[0] if is_single else embeddings

        except Exception as e:
            logger.error(f"Error embedding text with OpenAI: {e}")
            raise e

    def embed_image(self, image_input: Union[str, Image.Image]) -> List[float]:
        """
        Generate visual embeddings using Google SigLIP.
        Used for product images and image-to-image search.
        """
        try:
            img = self._load_image(image_input)
            model, processor = self.siglip
            
            # Preprocess image
            inputs = processor(images=img, return_tensors="pt").to(self.device)
            
            # Generate embedding
            with torch.no_grad():
                 image_features = model.get_image_features(**inputs)
            
            # Normalize embedding (critical for cosine similarity)
            image_features = image_features / image_features.norm(p=2, dim=-1, keepdim=True)
            
            return image_features[0].cpu().tolist()

        except Exception as e:
            logger.error(f"Error embedding image with SigLIP: {e}")
            raise e

    def embed_query_for_image_search(self, text: str) -> List[float]:
        """
        Embed a text query using the SigLIP model.
        This vector can be used to search against the *image embeddings*.
        (i.e. 'Find images that look like this text description')
        """
        try:
            from transformers import AutoProcessor
            
            # Lazy load model if not already loaded
            if self._siglip_model is None:
                 _, _ = self.siglip

            # Use SigLIP processor for text
            inputs = self._siglip_processor(text=[text], return_tensors="pt", padding="max_length").to(self.device)
            
            with torch.no_grad():
                # Get text features from SigLIP
                text_features = self._siglip_model.get_text_features(**inputs)
            
            # Normalize embedding
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
                    response = httpx.get(image_input)
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
