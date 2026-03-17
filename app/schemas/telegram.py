from pydantic import BaseModel, ConfigDict, model_validator
from typing import Optional


class TelegramUser(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    is_bot: bool = False
    first_name: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_id_to_str(cls, data):
        if isinstance(data, dict) and "id" in data:
            data["id"] = str(data["id"])
        return data


class TelegramChat(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    type: str = "private"
    first_name: Optional[str] = None


class TelegramVoice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file_id: str
    file_unique_id: str = ""
    duration: int
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


class TelegramAudio(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file_id: str
    file_unique_id: str = ""
    duration: int = 0
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    title: Optional[str] = None
    file_name: Optional[str] = None


class TelegramMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    message_id: int
    chat: TelegramChat
    from_user: Optional[TelegramUser] = None
    text: Optional[str] = None
    voice: Optional[TelegramVoice] = None
    audio: Optional[TelegramAudio] = None

    @model_validator(mode="before")
    @classmethod
    def _rename_from(cls, data):
        if isinstance(data, dict) and "from" in data:
            data["from_user"] = data.pop("from")
        return data


class TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    update_id: int
    message: Optional[TelegramMessage] = None


class TelegramWebhookResponse(BaseModel):
    ok: bool
    message: str
