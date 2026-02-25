from openai import OpenAI
from app.config.settings import settings
from app.utils.logger import get_logger
from app.prompts.image_captioner_prompt import IMAGE_CAPTION_PROMPT

logger = get_logger(__name__)

class ImageCaptionService:
    def __init__(self):
        self.client = OpenAI(api_key=settings.openai_api_key)

    def caption_image(self, image_url: str) -> str:
        """
        Use OpenAI Vision to generate a rich, structured description of the image.
        Uses the shared caption prompt for consistency.
        """
        if not image_url:
            return ""

        try:
            logger.info(f"Generating caption for image: {image_url}")
            response = self.client.chat.completions.create(
                model=settings.caption_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": IMAGE_CAPTION_PROMPT},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ]
                    },
                ],
                temperature=0.2,
                max_tokens=200,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"Error generating caption for {image_url}: {e}")
            return ""

image_caption_service = ImageCaptionService()
