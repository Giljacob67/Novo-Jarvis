import base64
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


def _get_openai_client():
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=settings.openai_api_key)


def _extract_response_text(response: Any) -> str:
    final_text = ""
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", "") != "message":
            continue
        for part in getattr(item, "content", []) or []:
            if getattr(part, "type", "") == "output_text":
                final_text += getattr(part, "text", "")
    return final_text.strip()


async def extract_text_from_image_bytes(image_bytes: bytes) -> dict[str, Any]:
    if not settings.openai_api_key:
        return {"error": "OPENAI_API_KEY não configurada para leitura de imagem.", "text": None}

    if not image_bytes:
        return {"error": "Imagem vazia.", "text": None}

    try:
        client = _get_openai_client()
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:image/jpeg;base64,{image_b64}"
        response = await client.responses.create(
            model=settings.openai_vision_model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Extraia o texto visível da imagem em português do Brasil. "
                                "Mantenha datas, números de processo e prazos. "
                                "Retorne apenas o texto extraído, sem explicações."
                            ),
                        },
                        {"type": "input_image", "image_url": data_url},
                    ],
                }
            ],
        )
        text = _extract_response_text(response)
        return {"text": text, "error": None}
    except Exception as e:
        logger.exception("Image OCR extraction failed")
        return {"error": f"Erro ao ler imagem: {e}", "text": None}
