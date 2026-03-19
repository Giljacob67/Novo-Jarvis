import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.telegram.org/bot{token}"
_FILE_URL = "https://api.telegram.org/file/bot{token}/{file_path}"

_TELEGRAM_MAX_LEN = 4096


def _split_message(text: str, max_len: int = _TELEGRAM_MAX_LEN) -> list[str]:
    """Split a long message into chunks that respect Telegram's character limit.

    Splits preferably on paragraph breaks, then newlines, then spaces,
    so words are never cut in the middle.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        # Try to break at a paragraph boundary first
        cut = text.rfind("\n\n", 0, max_len)
        if cut == -1:
            # Fallback: last newline
            cut = text.rfind("\n", 0, max_len)
        if cut == -1:
            # Fallback: last space
            cut = text.rfind(" ", 0, max_len)
        if cut == -1:
            # Hard cut — no whitespace found
            cut = max_len

        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()

    return [c for c in chunks if c]


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
        """Send a text message, splitting into multiple chunks if it exceeds Telegram's 4096-char limit."""
        url = f"{self._base}/sendMessage"
        chunks = _split_message(text)
        logger.info(
            "Sending message to chat_id=%s (len=%d, chunks=%d)",
            chat_id, len(text), len(chunks),
        )
        last_response: dict[str, Any] = {}
        for chunk in chunks:
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
            resp = await self.client.post(url, json=payload)
            if resp.status_code == 400:
                # Fallback: Markdown parse error → retry as plain text
                logger.warning(
                    "Telegram rejected Markdown chunk (chat_id=%s, len=%d). "
                    "Falling back to plain text.",
                    chat_id, len(chunk),
                )
                fallback_payload = {"chat_id": chat_id, "text": chunk}
                resp = await self.client.post(url, json=fallback_payload)
            resp.raise_for_status()
            last_response = resp.json()
        return last_response

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
