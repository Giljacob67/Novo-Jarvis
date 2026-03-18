from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


VALID_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
ALLOWED_USER_ID = 12345


def _make_payload(update_id, text=None, caption=None, photo=None, voice=None, user_id=ALLOWED_USER_ID):
    msg = {
        "message_id": 100 + update_id,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
    }
    if text is not None:
        msg["text"] = text
    if caption is not None:
        msg["caption"] = caption
    if photo is not None:
        msg["photo"] = photo
    if voice is not None:
        msg["voice"] = voice
    return {"update_id": update_id, "message": msg}


def test_telegram_invalid_secret(client: TestClient) -> None:
    payload = _make_payload(1, text="Test")
    response = client.post(
        "/webhooks/telegram",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert response.status_code == 403


def test_telegram_missing_secret(client: TestClient) -> None:
    payload = _make_payload(2, text="Test")
    response = client.post("/webhooks/telegram", json=payload)
    assert response.status_code == 403


def test_telegram_valid_secret_text(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(3, text="/start")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["message"] == "processed"
    _patch_telegram_send.assert_called_once()


def test_telegram_unauthorized_user(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(4, text="Hello", user_id=99999)
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    assert response.json()["message"] == "ignored"
    _patch_telegram_send.assert_not_called()


def test_telegram_duplicate_update(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(5, text="/start")
    resp1 = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert resp1.status_code == 200
    assert resp1.json()["message"] == "processed"

    resp2 = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert resp2.status_code == 200
    assert resp2.json()["message"] == "duplicate"


def test_telegram_no_message(client: TestClient) -> None:
    payload = {"update_id": 6}
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    assert response.json()["message"] == "ignored"


@patch("app.services.audio_service.transcribe_file", new_callable=AsyncMock)
@patch("app.services.telegram_service.download_file", new_callable=AsyncMock)
def test_telegram_voice_message(mock_download, mock_transcribe, client: TestClient, _patch_telegram_send) -> None:
    mock_download.return_value = b"fake_audio"
    mock_transcribe.return_value = {"text": "teste voz", "raw_json": None, "error": None}
    payload = _make_payload(7, voice={"file_id": "abc123", "file_unique_id": "xyz", "duration": 5})
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    assert response.json()["message"] == "voice_processed"
    _patch_telegram_send.assert_called()
    call_args = _patch_telegram_send.call_args_list[0]
    assert "teste voz" in call_args[0][1].lower()


def test_telegram_start_command(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(8, text="/start")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    call_text = _patch_telegram_send.call_args[0][1]
    assert "Jarvis" in call_text


def test_telegram_help_command(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(9, text="/help")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    call_text = _patch_telegram_send.call_args[0][1]
    assert "/myday" in call_text


def test_telegram_myday_command(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(10, text="/myday")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    call_text = _patch_telegram_send.call_args[0][1]
    assert "Prioridades" in call_text


def test_telegram_briefing_command_has_criticality_blocks(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(153, text="/briefing")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    sent_text = _patch_telegram_send.call_args[0][1]
    assert "CRÍTICO" in sent_text or "ATENÇÃO ALTA" in sent_text
    assert "Próximas ações" in sent_text


def test_telegram_remember_command(client: TestClient, _patch_telegram_send, db_session) -> None:
    payload = _make_payload(11, text="/remember Comprar leite")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    call_text = _patch_telegram_send.call_args[0][1]
    assert "Comprar leite" in call_text

    from app.models.memory_item import MemoryItem
    items = db_session.query(MemoryItem).filter(MemoryItem.user_id == str(ALLOWED_USER_ID)).all()
    assert len(items) == 1
    assert items[0].content == "Comprar leite"


def test_telegram_memories_command_empty(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(12, text="/memories")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    call_text = _patch_telegram_send.call_args[0][1]
    assert "não tem" in call_text.lower() or "nenhuma" in call_text.lower() or "ainda" in call_text.lower()


def test_telegram_memories_command_with_data(client: TestClient, _patch_telegram_send, db_session) -> None:
    from app.services.memory_service import save_memory
    save_memory(db_session, str(ALLOWED_USER_ID), "Reunião às 15h", category="task", source="command")

    payload = _make_payload(13, text="/memories")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    call_text = _patch_telegram_send.call_args[0][1]
    assert "Reunião às 15h" in call_text


def test_telegram_free_text(client: TestClient, _patch_telegram_send) -> None:
    with patch("app.services.assistant_service._openai_service.generate_reply", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = "Olá! Como posso ajudar?"
        payload = _make_payload(14, text="Qual o meu dia hoje?")
        response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
        assert response.status_code == 200
        call_text = _patch_telegram_send.call_args[0][1]
        assert "Olá" in call_text
        mock_gen.assert_called_once()


def test_telegram_photo_caption_processed_as_text(client: TestClient, _patch_telegram_send) -> None:
    with patch("app.services.assistant_service._openai_service.generate_reply", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = "Tarefa criada para amanhã."
        payload = _make_payload(
            140,
            caption="agende essa tarefa para amanhã: verificar o processo anexo",
            photo=[{"file_id": "p1", "file_unique_id": "u1", "width": 640, "height": 480}],
        )
        response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
        assert response.status_code == 200
        assert response.json()["message"] == "processed"
        sent_text = _patch_telegram_send.call_args[0][1]
        assert "Tarefa criada" in sent_text
        mock_gen.assert_called_once()


@patch("app.routes.telegram.google_tasks_service.create_task", new_callable=AsyncMock)
@patch("app.routes.telegram._extract_photo_text", new_callable=AsyncMock)
def test_telegram_photo_deadline_auto_schedule(mock_ocr, mock_create_task, client: TestClient, _patch_telegram_send) -> None:
    mock_ocr.return_value = "Último Dia Prazo: 27 de março de 2026"
    mock_create_task.return_value = {"id": "task1", "title": "Cumprir prazo processual", "status": "needsAction"}
    payload = _make_payload(
        141,
        caption="agende esse prazo",
        photo=[{"file_id": "p2", "file_unique_id": "u2", "width": 1200, "height": 900}],
    )
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    assert response.json()["message"] == "processed"
    sent_text = _patch_telegram_send.call_args[0][1]
    assert "Prazo agendado para 2026-03-27" in sent_text
    mock_create_task.assert_called_once()


@patch("app.routes.telegram.google_tasks_service.create_task", new_callable=AsyncMock)
@patch("app.routes.telegram._extract_photo_text", new_callable=AsyncMock)
def test_telegram_schedule_uses_recent_ocr_context(mock_ocr, mock_create_task, client: TestClient, _patch_telegram_send) -> None:
    with patch("app.services.assistant_service._openai_service.generate_reply", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = "Entendi o print."
        mock_ocr.return_value = "Último Dia Prazo: 27 de março de 2026"
        mock_create_task.return_value = {"id": "task2", "title": "Cumprir prazo processual", "status": "needsAction"}

        body1 = _make_payload(
            142,
            caption="segue o print do processo",
            photo=[{"file_id": "p3", "file_unique_id": "u3", "width": 1000, "height": 700}],
        )
        resp1 = client.post("/webhooks/telegram", json=body1, headers=VALID_HEADERS)
        assert resp1.status_code == 200

        body2 = _make_payload(143, text="agende esse prazo")
        resp2 = client.post("/webhooks/telegram", json=body2, headers=VALID_HEADERS)
        assert resp2.status_code == 200
        assert resp2.json()["message"] == "processed"

        sent_text = _patch_telegram_send.call_args[0][1]
        assert "Prazo agendado para 2026-03-27" in sent_text
        mock_create_task.assert_called_once()


def test_telegram_news_query_without_browser_is_deterministic(client: TestClient, _patch_telegram_send) -> None:
    from app.config import settings as app_settings

    with patch("app.services.assistant_service._openai_service.generate_reply", new_callable=AsyncMock) as mock_gen:
        old_enabled = app_settings.browser_automation_enabled
        old_domains = app_settings.browser_allowed_domains
        try:
            app_settings.browser_automation_enabled = False
            app_settings.browser_allowed_domains = ""
            payload = _make_payload(144, text="Busque notícias sobre o openclaw")
            response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
            assert response.status_code == 200
            sent_text = _patch_telegram_send.call_args[0][1].lower()
            assert "navegação web não está ativa" in sent_text or "não está ativa" in sent_text
            mock_gen.assert_not_called()
        finally:
            app_settings.browser_automation_enabled = old_enabled
            app_settings.browser_allowed_domains = old_domains


def test_telegram_myday_does_not_call_openai(client: TestClient, _patch_telegram_send) -> None:
    with patch("app.services.assistant_service._openai_service.generate_reply", new_callable=AsyncMock) as mock_gen:
        payload = _make_payload(15, text="/myday")
        response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
        assert response.status_code == 200
        call_text = _patch_telegram_send.call_args[0][1]
        assert "Prioridades" in call_text
        mock_gen.assert_not_called()


def test_telegram_focus_command(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(150, text="/focus")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    sent_text = _patch_telegram_send.call_args[0][1]
    assert "Top 3 focos" in sent_text


def test_telegram_autonomy_command(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(151, text="/autonomy hybrid_safe")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    sent_text = _patch_telegram_send.call_args[0][1]
    assert "Autonomia atual" in sent_text


def test_telegram_proactive_status_command(client: TestClient, _patch_telegram_send) -> None:
    payload = _make_payload(152, text="/proactivestatus")
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    sent_text = _patch_telegram_send.call_args[0][1]
    assert "Status Proativo" in sent_text


@patch("app.services.audio_service.transcribe_file", new_callable=AsyncMock)
@patch("app.services.telegram_service.download_file", new_callable=AsyncMock)
def test_telegram_voice_persists_metadata(mock_download, mock_transcribe, client: TestClient, _patch_telegram_send, db_session) -> None:
    mock_download.return_value = b"fake_audio"
    mock_transcribe.return_value = {"text": "teste metadata", "raw_json": None, "error": None}
    payload = _make_payload(16, voice={"file_id": "voice123", "file_unique_id": "uniq", "duration": 10, "mime_type": "audio/ogg"})
    response = client.post("/webhooks/telegram", json=payload, headers=VALID_HEADERS)
    assert response.status_code == 200
    assert response.json()["message"] == "voice_processed"

    from app.models.voice_message_log import VoiceMessageLog
    logs = db_session.query(VoiceMessageLog).filter(VoiceMessageLog.telegram_file_id == "voice123").all()
    assert len(logs) == 1
    assert logs[0].duration_seconds == 10
    assert logs[0].transcription_text == "teste metadata"
