import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.telegram.org/bot{token}"
_FILE_URL = "https://api.telegram.org/file/bot{token}/{file_path}"


class TelegramService:
    def __init__(self, bot_token: str) -> None:
        self._token = bot_token
        self._base = _BASE_URL.format(token=bot_token)
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("TelegramService not started. Call start() first.")
        return self._client

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        url = f"{self._base}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        logger.info("Sending message to chat_id=%s (len=%d)", chat_id, len(text))
        resp = await self.client.post(url, json=payload)
        if resp.status_code == 400:
            # Fallback for Markdown entity parsing errors or other formatting issues.
            logger.warning(
                "Telegram rejected Markdown message (chat_id=%s). Falling back to plain text.",
                chat_id,
            )
            fallback_payload = {"chat_id": chat_id, "text": text}
            resp = await self.client.post(url, json=fallback_payload)
        resp.raise_for_status()
        return resp.json()

    async def set_webhook(self, url: str, secret_token: str = "") -> dict[str, Any]:
        api_url = f"{self._base}/setWebhook"
        payload: dict[str, Any] = {"url": url}
        if secret_token:
            payload["secret_token"] = secret_token
        logger.info("Setting webhook to url=%s", url)
        resp = await self.client.post(api_url, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def get_webhook_info(self) -> dict[str, Any]:
        url = f"{self._base}/getWebhookInfo"
        resp = await self.client.get(url)
        resp.raise_for_status()
        return resp.json()

    async def get_file(self, file_id: str) -> dict[str, Any]:
        url = f"{self._base}/getFile"
        resp = await self.client.post(url, json={"file_id": file_id})
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {})

    async def download_file(self, file_id: str) -> bytes:
        file_info = await self.get_file(file_id)
        file_path = file_info.get("file_path", "")
        if not file_path:
            raise ValueError(f"No file_path returned for file_id={file_id}")
        download_url = _FILE_URL.format(token=self._token, file_path=file_path)
        logger.info("Downloading file file_id=%s", file_id)
        resp = await self.client.get(download_url)
        resp.raise_for_status()
        return resp.content

    async def send_voice(self, chat_id: int, audio_bytes: bytes, caption: str | None = None) -> dict[str, Any]:
        url = f"{self._base}/sendVoice"
        files = {"voice": ("voice.ogg", audio_bytes, "audio/ogg")}
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        logger.info("Sending voice to chat_id=%s (size=%d bytes)", chat_id, len(audio_bytes))
        resp = await self.client.post(url, data=data, files=files)
        resp.raise_for_status()
        return resp.json()

    async def send_audio(self, chat_id: int, audio_bytes: bytes, caption: str | None = None, filename: str = "response.mp3") -> dict[str, Any]:
        url = f"{self._base}/sendAudio"
        mime = "audio/ogg" if filename.endswith(".ogg") else "audio/mpeg"
        files = {"audio": (filename, audio_bytes, mime)}
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        logger.info("Sending audio to chat_id=%s (size=%d bytes)", chat_id, len(audio_bytes))
        resp = await self.client.post(url, data=data, files=files)
        resp.raise_for_status()
        return resp.json()
