"""
browser_service.py — Phase 7 browser automation service.

Architecture:
  - One shared Playwright Browser per worker process (started in app lifespan)
  - One BrowserContext per BrowserSession (isolated, non-persistent)
  - Domain allowlist enforced on every navigation (empty list = all blocked)
  - Sensitive actions create a PendingApproval and return without executing
  - Login pages detected heuristically; session paused for manual resume
  - Downloads use page.expect_download() + download.save_as()

TODO (future): support optional persistent storage state per session for
  supervised long-lived sessions (cookies/localStorage restored on resume).
"""

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models.browser_artifact import BrowserArtifact
from app.models.browser_session import BrowserSession
from app.models.browser_step_log import BrowserStepLog
from app.utils.browser_utils import (
    ensure_dirs,
    is_domain_allowed,
    is_login_page,
    is_sensitive_action,
    log_approval_created,
    log_click,
    log_download_completed,
    log_fill,
    log_navigation,
    log_sensitive_action_blocked,
    log_session_closed,
    log_session_failed,
    log_session_started,
    sanitize_url_for_logs,
    summarize_page_text,
)

logger = logging.getLogger(__name__)

_browser = None
_playwright = None

_live_contexts: dict[str, Any] = {}


async def start_browser() -> None:
    global _browser, _playwright
    if not settings.browser_automation_enabled:
        logger.info("Browser automation disabled — skipping browser launch")
        return
    try:
        from playwright.async_api import async_playwright
        ensure_dirs()
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=settings.browser_headless)
        logger.info("Playwright shared browser launched (headless=%s)", settings.browser_headless)
    except Exception:
        logger.exception("Failed to launch Playwright browser")
        _browser = None
        _playwright = None


async def stop_browser() -> None:
    global _browser, _playwright
    for sid in list(_live_contexts.keys()):
        try:
            ctx = _live_contexts.pop(sid, None)
            if ctx:
                await ctx.close()
        except Exception:
            logger.warning("Error closing context for session %s during shutdown", sid)
    if _browser:
        try:
            await _browser.close()
        except Exception:
            logger.warning("Error closing Playwright browser")
        _browser = None
    if _playwright:
        try:
            await _playwright.stop()
        except Exception:
            logger.warning("Error stopping Playwright instance")
        _playwright = None
    logger.info("Playwright browser stopped")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return str(uuid.uuid4())[:8]


def _session_expires_at() -> datetime:
    return _utcnow() + timedelta(minutes=settings.browser_session_ttl_minutes)


def _log_step(
    db: Session,
    session: BrowserSession,
    action_type: str,
    *,
    selector: str | None = None,
    value: str | None = None,
    url: str | None = None,
    result: str | None = None,
    status: str = "ok",
    error_message: str | None = None,
    screenshot_path: str | None = None,
) -> None:
    step = BrowserStepLog(
        session_id=session.session_id,
        user_id=session.user_id,
        step_number=session.steps_taken,
        action_type=action_type,
        selector=selector,
        value=value,
        url=url,
        result=result,
        status=status,
        error_message=error_message,
        screenshot_path=screenshot_path,
    )
    db.add(step)
    db.commit()


def _check_browser_ready() -> dict | None:
    if not settings.browser_automation_enabled:
        return {"error": "Automação de browser está desativada."}
    if _browser is None:
        return {"error": "Browser não está disponível. Reinicie o Jarvis."}
    return None


def _check_domain(url: str) -> dict | None:
    if not is_domain_allowed(url):
        domain_list = settings.browser_allowed_domains.strip()
        if not domain_list:
            return {
                "error": (
                    "Nenhum domínio está na lista de permissões (BROWSER_ALLOWED_DOMAINS está vazio). "
                    "Configure os domínios permitidos antes de usar automação de browser."
                )
            }
        return {
            "error": (
                f"Domínio não permitido: {sanitize_url_for_logs(url)}. "
                f"Domínios permitidos: {domain_list}"
            )
        }
    return None


async def start_session(db: Session, user_id: str, start_url: str) -> dict:
    err = _check_browser_ready()
    if err:
        return err

    existing = db.query(BrowserSession).filter(
        BrowserSession.user_id == user_id,
        BrowserSession.status.in_(["active", "paused_for_login"]),
    ).first()
    if existing:
        return {
            "error": (
                f"Você já tem uma sessão ativa (id={existing.session_id}, "
                f"url={sanitize_url_for_logs(existing.current_url or existing.start_url or '')}). "
                f"Use /browserclose {existing.session_id} para encerrar ou "
                f"/browserstatus para verificar."
            )
        }

    domain_err = _check_domain(start_url)
    if domain_err:
        return domain_err

    context = await _browser.new_context()
    page = await context.new_page()
    page.set_default_timeout(settings.browser_default_timeout_ms)
    page.set_default_navigation_timeout(settings.browser_navigation_timeout_ms)

    session_id = _new_session_id()
    session = BrowserSession(
        session_id=session_id,
        user_id=user_id,
        status="active",
        start_url=start_url,
        current_url=start_url,
        expires_at=_session_expires_at(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    _live_contexts[session_id] = (context, page)

    log_session_started(db, session_id, user_id, start_url)
    logger.info("Browser session started id=%s user=%s", session_id, user_id)
    return {"session_id": session_id, "status": "active", "start_url": start_url}


async def close_session(db: Session, user_id: str, session_id: str,
                        reason: str = "user") -> dict:
    session = db.query(BrowserSession).filter(
        BrowserSession.session_id == session_id,
        BrowserSession.user_id == user_id,
    ).first()
    if not session:
        return {"error": "Sessão não encontrada."}
    if session.status in ("closed", "expired"):
        return {"status": session.status, "message": "Sessão já encerrada."}

    pair = _live_contexts.pop(session_id, None)
    if pair:
        try:
            ctx, _ = pair
            await ctx.close()
        except Exception:
            logger.warning("Error closing context for session %s", session_id)

    session.status = "closed"
    session.closed_at = _utcnow()
    db.commit()
    log_session_closed(db, session_id, reason=reason)
    return {"status": "closed", "session_id": session_id}


def get_session(db: Session, user_id: str, session_id: str) -> BrowserSession | None:
    return db.query(BrowserSession).filter(
        BrowserSession.session_id == session_id,
        BrowserSession.user_id == user_id,
    ).first()


def list_sessions(db: Session, user_id: str) -> list[BrowserSession]:
    return (
        db.query(BrowserSession)
        .filter(BrowserSession.user_id == user_id)
        .order_by(BrowserSession.created_at.desc())
        .limit(10)
        .all()
    )


def _get_live_page(session_id: str):
    pair = _live_contexts.get(session_id)
    if pair is None:
        return None, None
    return pair


async def open_url(db: Session, user_id: str, session_id: str, url: str) -> dict:
    session = get_session(db, user_id, session_id)
    if not session:
        return {"error": "Sessão não encontrada."}
    if session.status not in ("active",):
        return {"error": f"Sessão com status '{session.status}'. Não pode navegar agora."}

    domain_err = _check_domain(url)
    if domain_err:
        log_navigation(db, session_id, url, status="blocked", error=domain_err["error"])
        return domain_err

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página não disponível. A sessão pode ter expirado."}
    _, page = pair

    try:
        await page.goto(url, wait_until="domcontentloaded")
        title = await page.title()
        current_url = page.url

        has_password = await page.evaluate(
            "() => !!document.querySelector('input[type=password]')"
        )

        if is_login_page(current_url, title, has_password_input=has_password):
            session.status = "paused_for_login"
            session.current_url = current_url
            session.page_title = title
            session.updated_at = _utcnow()
            db.commit()
            _log_step(db, session, "navigate", url=sanitize_url_for_logs(url),
                      result="login_detected", status="paused")
            log_navigation(db, session_id, url, status="paused_login")
            return {
                "status": "paused_for_login",
                "message": (
                    "⚠️ Página de login detectada. O Jarvis pausou a sessão.\n"
                    f"Faça o login manualmente e depois use /browserresume {session_id} para continuar."
                ),
                "url": sanitize_url_for_logs(current_url),
                "title": title,
            }

        session.current_url = current_url
        session.page_title = title
        session.steps_taken += 1
        session.updated_at = _utcnow()
        db.commit()

        _log_step(db, session, "navigate", url=sanitize_url_for_logs(url),
                  result=f"title={title}", status="ok")
        log_navigation(db, session_id, url, status="ok")
        return {"status": "ok", "url": sanitize_url_for_logs(current_url), "title": title}
    except Exception as exc:
        log_navigation(db, session_id, url, status="error", error=str(exc))
        log_session_failed(db, session_id, str(exc))
        return {"error": f"Erro ao navegar: {exc}"}


async def capture_screenshot(db: Session, user_id: str, session_id: str) -> dict:
    session = get_session(db, user_id, session_id)
    if not session:
        return {"error": "Sessão não encontrada."}

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página não disponível."}
    _, page = pair

    try:
        fname = f"screen_{session_id}_{int(_utcnow().timestamp())}.png"
        fpath = os.path.join(settings.browser_screenshot_dir, fname)
        await page.screenshot(path=fpath, full_page=False)

        artifact = BrowserArtifact(
            session_id=session_id,
            user_id=user_id,
            artifact_type="screenshot",
            file_path=fpath,
            url=session.current_url,
            mime_type="image/png",
        )
        db.add(artifact)
        session.last_screenshot_path = fpath
        session.steps_taken += 1
        db.commit()

        _log_step(db, session, "screenshot", screenshot_path=fpath, status="ok")
        return {"status": "ok", "file_path": fpath, "artifact_id": artifact.id}
    except Exception as exc:
        return {"error": f"Erro ao capturar screenshot: {exc}"}


async def extract_visible_text(db: Session, user_id: str, session_id: str) -> dict:
    session = get_session(db, user_id, session_id)
    if not session:
        return {"error": "Sessão não encontrada."}

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página não disponível."}
    _, page = pair

    try:
        raw = await page.evaluate("() => document.body.innerText")
        summary = summarize_page_text(raw)
        session.steps_taken += 1
        db.commit()
        _log_step(db, session, "extract_text", result=f"{len(raw)} chars", status="ok")
        return {"status": "ok", "text": summary, "total_chars": len(raw)}
    except Exception as exc:
        return {"error": f"Erro ao extrair texto: {exc}"}


async def _maybe_require_approval(
    db: Session,
    session: BrowserSession,
    action_type: str,
    selector: str | None,
    value: str | None,
    current_url: str | None,
    screenshot_path: str | None = None,
) -> dict | None:
    if not is_sensitive_action(action_type, selector, value, current_url):
        return None
    if action_type == "browser_click" and not settings.browser_require_approval_for_submit:
        return None

    log_sensitive_action_blocked(db, session.session_id, action_type, selector, current_url)

    from app.services.approval_service import create_pending_approval
    approval = create_pending_approval(
        db,
        user_id=session.user_id,
        action_type=action_type,
        title=f"Ação de browser: {action_type}",
        summary=(
            f"Sessão: {session.session_id}\n"
            f"URL: {sanitize_url_for_logs(current_url or '')}\n"
            f"Seletor: {selector}\n"
            f"Valor: {value}"
        ),
        payload={
            "session_id": session.session_id,
            "action_type": action_type,
            "selector": selector,
            "value": value,
            "current_url": current_url,
            "screenshot_path": screenshot_path,
        },
        source="browser",
    )
    if approval:
        log_approval_created(db, session.session_id, approval.id, action_type)
        return {
            "status": "pending_approval",
            "approval_id": approval.id,
            "message": (
                f"⚠️ Ação sensível requer aprovação.\n"
                f"Use /approve {approval.id} para confirmar ou /reject {approval.id} para cancelar."
            ),
        }
    return {"error": "Não foi possível criar aprovação para ação sensível."}


async def click(db: Session, user_id: str, session_id: str, selector: str) -> dict:
    session = get_session(db, user_id, session_id)
    if not session or session.status != "active":
        return {"error": "Sessão não disponível para ações."}

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página não disponível."}
    _, page = pair

    approval_required = await _maybe_require_approval(
        db, session, "browser_click", selector, None, session.current_url
    )
    if approval_required:
        return approval_required

    try:
        await page.click(selector)
        session.steps_taken += 1
        session.current_url = page.url
        db.commit()
        _log_step(db, session, "click", selector=selector, status="ok")
        log_click(db, session_id, selector, status="ok")
        return {"status": "ok", "selector": selector}
    except Exception as exc:
        _log_step(db, session, "click", selector=selector, status="error",
                  error_message=str(exc))
        log_click(db, session_id, selector, status="error")
        return {"error": f"Erro ao clicar em '{selector}': {exc}"}


async def fill(db: Session, user_id: str, session_id: str,
               selector: str, value: str) -> dict:
    session = get_session(db, user_id, session_id)
    if not session or session.status != "active":
        return {"error": "Sessão não disponível para ações."}

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página não disponível."}
    _, page = pair

    try:
        await page.fill(selector, value)
        session.steps_taken += 1
        db.commit()
        _log_step(db, session, "fill", selector=selector, value="[redacted]", status="ok")
        log_fill(db, session_id, selector, status="ok")
        return {"status": "ok", "selector": selector}
    except Exception as exc:
        _log_step(db, session, "fill", selector=selector, status="error",
                  error_message=str(exc))
        log_fill(db, session_id, selector, status="error")
        return {"error": f"Erro ao preencher '{selector}': {exc}"}


async def select_option(db: Session, user_id: str, session_id: str,
                        selector: str, value: str) -> dict:
    session = get_session(db, user_id, session_id)
    if not session or session.status != "active":
        return {"error": "Sessão não disponível para ações."}

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página não disponível."}
    _, page = pair

    try:
        await page.select_option(selector, value)
        session.steps_taken += 1
        db.commit()
        _log_step(db, session, "select_option", selector=selector, value=value, status="ok")
        return {"status": "ok", "selector": selector, "value": value}
    except Exception as exc:
        return {"error": f"Erro ao selecionar opção em '{selector}': {exc}"}


async def press(db: Session, user_id: str, session_id: str,
                selector: str, key: str) -> dict:
    session = get_session(db, user_id, session_id)
    if not session or session.status != "active":
        return {"error": "Sessão não disponível para ações."}

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página não disponível."}
    _, page = pair

    try:
        await page.press(selector, key)
        session.steps_taken += 1
        db.commit()
        _log_step(db, session, "press", selector=selector, value=key, status="ok")
        return {"status": "ok", "selector": selector, "key": key}
    except Exception as exc:
        return {"error": f"Erro ao pressionar tecla '{key}' em '{selector}': {exc}"}


async def wait_for_selector(db: Session, user_id: str, session_id: str,
                            selector: str, timeout_ms: int | None = None) -> dict:
    session = get_session(db, user_id, session_id)
    if not session or session.status != "active":
        return {"error": "Sessão não disponível para ações."}

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página não disponível."}
    _, page = pair

    try:
        opts = {"timeout": timeout_ms} if timeout_ms else {}
        await page.wait_for_selector(selector, **opts)
        session.steps_taken += 1
        db.commit()
        _log_step(db, session, "wait_for_selector", selector=selector, status="ok")
        return {"status": "ok", "selector": selector}
    except Exception as exc:
        return {"error": f"Timeout aguardando '{selector}': {exc}"}


async def download_file(db: Session, user_id: str, session_id: str,
                        trigger_selector: str) -> dict:
    if not settings.browser_allow_file_downloads:
        return {"error": "Download de arquivos está desativado."}

    session = get_session(db, user_id, session_id)
    if not session or session.status != "active":
        return {"error": "Sessão não disponível para ações."}

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página não disponível."}
    _, page = pair

    try:
        async with page.expect_download() as dl_info:
            await page.click(trigger_selector)
        download = await dl_info.value

        fname = download.suggested_filename or f"download_{session_id}_{int(_utcnow().timestamp())}"
        fpath = os.path.join(settings.browser_download_dir, fname)
        await download.save_as(fpath)
        size = os.path.getsize(fpath) if os.path.exists(fpath) else None

        artifact = BrowserArtifact(
            session_id=session_id,
            user_id=user_id,
            artifact_type="download",
            file_path=fpath,
            url=session.current_url,
            file_size_bytes=size,
        )
        db.add(artifact)
        session.steps_taken += 1
        db.commit()

        _log_step(db, session, "download", selector=trigger_selector,
                  result=fpath, status="ok")
        log_download_completed(db, session_id, fpath, session.current_url)
        return {"status": "ok", "file_path": fpath, "size_bytes": size, "artifact_id": artifact.id}
    except Exception as exc:
        return {"error": f"Erro ao fazer download: {exc}"}


async def get_page_summary(db: Session, user_id: str, session_id: str) -> dict:
    text_result = await extract_visible_text(db, user_id, session_id)
    if "error" in text_result:
        return text_result
    session = get_session(db, user_id, session_id)
    return {
        "status": "ok",
        "url": sanitize_url_for_logs(session.current_url or "") if session else "",
        "title": session.page_title if session else "",
        "summary": text_result["text"],
    }


async def resume_session(db: Session, user_id: str, session_id: str) -> dict:
    session = get_session(db, user_id, session_id)
    if not session:
        return {"error": "Sessão não encontrada."}
    if session.status != "paused_for_login":
        return {"error": f"Sessão não está pausada para login (status: {session.status})."}

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Contexto de browser não disponível. Sessão pode ter expirado."}
    _, page = pair

    try:
        current_url = page.url
        title = await page.title()
        has_password = await page.evaluate(
            "() => !!document.querySelector('input[type=password]')"
        )
        if is_login_page(current_url, title, has_password_input=has_password):
            return {
                "status": "still_on_login",
                "message": "A página ainda parece ser uma tela de login. Conclua o login e tente novamente.",
            }
        session.status = "active"
        session.current_url = current_url
        session.page_title = title
        session.updated_at = _utcnow()
        db.commit()
        return {"status": "resumed", "url": sanitize_url_for_logs(current_url), "title": title}
    except Exception as exc:
        return {"error": f"Erro ao verificar sessão: {exc}"}


async def approve_and_execute_browser_action(
    db: Session, user_id: str, session_id: str, approval_id: int, payload: dict
) -> dict:
    session = get_session(db, user_id, session_id)
    if not session:
        return {"error": "Sessão de browser não encontrada."}
    if session.status != "active":
        return {"error": f"Sessão não está ativa (status: {session.status})."}

    action_type = payload.get("action_type", "")
    selector = payload.get("selector")
    value = payload.get("value")

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página de browser não disponível."}
    _, page = pair

    try:
        if action_type in ("browser_click", "browser_submit_form"):
            await page.click(selector)
            session.steps_taken += 1
            session.current_url = page.url
            db.commit()
            _log_step(db, session, action_type, selector=selector,
                      result="executed_after_approval", status="ok")
            return {"status": "ok", "action": action_type, "selector": selector}

        if action_type == "browser_fill":
            await page.fill(selector, value or "")
            session.steps_taken += 1
            db.commit()
            _log_step(db, session, action_type, selector=selector,
                      value="[redacted]", result="executed_after_approval", status="ok")
            return {"status": "ok", "action": action_type, "selector": selector}

        return {"error": f"Tipo de ação de browser não suportado: {action_type}"}
    except Exception as exc:
        return {"error": f"Erro ao executar ação aprovada: {exc}"}


async def expire_old_sessions(db: Session) -> int:
    now = _utcnow()
    active = db.query(BrowserSession).filter(
        BrowserSession.status.in_(["active", "paused_for_login"]),
    ).all()
    expired_count = 0
    for session in active:
        if session.expires_at and session.is_expired():
            pair = _live_contexts.pop(session.session_id, None)
            if pair:
                try:
                    ctx, _ = pair
                    await ctx.close()
                except Exception:
                    logger.warning("Error closing expired context %s", session.session_id)
            session.status = "expired"
            session.closed_at = now
            expired_count += 1
    if expired_count:
        db.commit()
        logger.info("Expired %d browser session(s)", expired_count)
    return expired_count


async def get_interactive_elements(db: Session, user_id: str, session_id: str) -> dict:
    session = get_session(db, user_id, session_id)
    if not session:
        return {"error": "Sessão não encontrada."}

    pair = _get_live_page(session_id)
    if not pair[1]:
        return {"error": "Página não disponível."}
    _, page = pair

    try:
        # Script to find buttons, links, and inputs
        script = """
        () => {
            const elements = Array.from(document.querySelectorAll('button, a, input, select, textarea, [role="button"]'));
            return elements.map(el => {
                let rect = el.getBoundingClientRect();
                return {
                    tag: el.tagName.toLowerCase(),
                    text: el.innerText.trim() || el.value || el.placeholder || el.ariaLabel || '',
                    type: el.type || '',
                    id: el.id,
                    ariaLabel: el.ariaLabel,
                    isVisible: rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== 'none',
                };
            }).filter(el => el.isVisible && (el.text || el.id));
        }
        """
        raw_elements = await page.evaluate(script)
        
        # Format for readability
        formatted = []
        for el in raw_elements:
            desc = f"[{el['tag']}] {el['text']}"
            if el['id']:
                desc += f" (id: {el['id']})"
            formatted.append(desc)

        session.steps_taken += 1
        db.commit()
        return {"status": "ok", "elements": formatted[:50]} # Limit to 50 for brevity
    except Exception as exc:
        return {"error": f"Erro ao extrair elementos interativos: {exc}"}
