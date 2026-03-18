#!/usr/bin/env python3
"""Smoke test rapido para deploy Railway do Jarvis.

Uso basico:
    APP_BASE_URL="https://novo-jarvis-production.up.railway.app" \
    TELEGRAM_BOT_TOKEN="..." \
    TELEGRAM_WEBHOOK_SECRET="..." \
    TELEGRAM_ALLOWED_USER_ID="7995994992" \
    python scripts/railway_smoke_test.py

Opcional:
    --fix-webhook      Re-registra o webhook se a URL estiver divergente
    --no-simulate      Nao envia update fake para /webhooks/telegram
    --command "/focus" Comando usado no update fake (padrao: /proactivestatus)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - ambiente sem dependencias
    httpx = None  # type: ignore[assignment]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _base_url_from_env() -> str:
    raw = os.environ.get("APP_BASE_URL", "").strip()
    return raw.rstrip("/")


def _print_result(result: CheckResult) -> None:
    prefix = "[OK]" if result.ok else "[FAIL]"
    print(f"{prefix} {result.name}: {result.detail}")


def check_health(client: httpx.Client, base_url: str) -> CheckResult:
    url = f"{base_url}/health"
    try:
        resp = client.get(url)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        ok = resp.status_code == 200 and data.get("status") == "ok"
        return CheckResult("health", ok, f"HTTP {resp.status_code} body={data}")
    except Exception as exc:
        return CheckResult("health", False, f"erro: {exc}")


def get_webhook_info(client: httpx.Client, token: str) -> tuple[bool, dict[str, Any] | None, str]:
    url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    try:
        resp = client.get(url)
        data = resp.json()
    except Exception as exc:
        return False, None, f"erro ao consultar Telegram API: {exc}"

    if resp.status_code != 200 or not data.get("ok"):
        return False, None, f"HTTP {resp.status_code} body={data}"
    return True, data.get("result", {}), "ok"


def set_webhook(client: httpx.Client, token: str, webhook_url: str, secret: str) -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{token}/setWebhook"
    payload: dict[str, Any] = {"url": webhook_url}
    if secret:
        payload["secret_token"] = secret
    try:
        resp = client.post(url, json=payload)
        data = resp.json()
    except Exception as exc:
        return False, f"erro ao registrar webhook: {exc}"
    if resp.status_code != 200 or not data.get("ok"):
        return False, f"HTTP {resp.status_code} body={data}"
    return True, data.get("description", "ok")


def simulate_webhook_update(
    client: httpx.Client,
    base_url: str,
    secret: str,
    from_user_id: str,
    chat_id: str,
    command_text: str,
) -> CheckResult:
    url = f"{base_url}/webhooks/telegram"
    update_id = int(time.time() * 1000)
    payload = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "text": command_text,
            "chat": {
                "id": int(chat_id),
                "type": "private",
            },
            "from": {
                "id": int(from_user_id),
                "is_bot": False,
                "first_name": "Smoke",
            },
        },
    }
    headers: dict[str, str] = {"content-type": "application/json"}
    if secret:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret

    try:
        resp = client.post(url, content=json.dumps(payload), headers=headers)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as exc:
        return CheckResult("webhook_simulation", False, f"erro: {exc}")

    ok = resp.status_code == 200 and data.get("ok") is True and data.get("message") in {"processed", "duplicate"}
    return CheckResult("webhook_simulation", ok, f"HTTP {resp.status_code} body={data}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test do Jarvis no Railway")
    parser.add_argument("--fix-webhook", action="store_true", help="Corrige webhook automaticamente se divergente")
    parser.add_argument("--no-simulate", action="store_true", help="Nao executa POST fake no /webhooks/telegram")
    parser.add_argument("--command", default="/proactivestatus", help="Comando no update fake (padrao: /proactivestatus)")
    parser.add_argument("--allowed-user-id", default=os.environ.get("TELEGRAM_ALLOWED_USER_ID", ""))
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--timeout", type=float, default=25.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = _base_url_from_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    allowed_user_id = (args.allowed_user_id or "").strip()
    chat_id = (args.chat_id or allowed_user_id).strip()

    if not base_url:
        print("APP_BASE_URL nao definido. Exemplo: https://novo-jarvis-production.up.railway.app")
        return 1

    expected_webhook_url = f"{base_url}/webhooks/telegram"
    print(f"Base URL: {base_url}")
    print(f"Webhook esperado: {expected_webhook_url}")

    if httpx is None:
        print("Dependencia ausente: httpx")
        print("Instale com: pip install -r requirements.txt")
        return 1

    results: list[CheckResult] = []
    with httpx.Client(timeout=args.timeout) as client:
        results.append(check_health(client, base_url))

        if token:
            ok_info, info, info_msg = get_webhook_info(client, token)
            if not ok_info or info is None:
                results.append(CheckResult("telegram_webhook_info", False, info_msg))
            else:
                current_url = info.get("url", "")
                last_error = info.get("last_error_message", "")
                pending = info.get("pending_update_count", 0)
                url_ok = current_url == expected_webhook_url
                results.append(
                    CheckResult(
                        "telegram_webhook_url",
                        url_ok,
                        f"url_atual={current_url} pending={pending} last_error={last_error or '-'}",
                    )
                )
                if args.fix_webhook and not url_ok:
                    ok_set, set_msg = set_webhook(client, token, expected_webhook_url, secret)
                    results.append(CheckResult("telegram_set_webhook", ok_set, set_msg))
        else:
            results.append(CheckResult("telegram_webhook_url", False, "TELEGRAM_BOT_TOKEN ausente"))

        if not args.no_simulate:
            if not allowed_user_id or not chat_id:
                results.append(
                    CheckResult(
                        "webhook_simulation",
                        False,
                        "TELEGRAM_ALLOWED_USER_ID (ou --allowed-user-id/--chat-id) ausente",
                    )
                )
            else:
                results.append(
                    simulate_webhook_update(
                        client=client,
                        base_url=base_url,
                        secret=secret,
                        from_user_id=allowed_user_id,
                        chat_id=chat_id,
                        command_text=args.command,
                    )
                )

    print("\nResumo:")
    for item in results:
        _print_result(item)

    failed = [r for r in results if not r.ok]
    if failed:
        print(f"\nFalhas: {len(failed)}")
        return 1

    print("\nSmoke test concluido com sucesso.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
