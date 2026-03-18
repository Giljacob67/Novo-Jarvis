"""dashboard.py — Read-only web dashboard for Jarvis.

Serves HTML pages at /dashboard/* with live data from the database.
Protected by a simple token query param or cookie (?token=<DASHBOARD_TOKEN>).
No build step required — vanilla HTML/CSS/JS only.

Pages:
    GET  /dashboard              → Overview (stats, last 5 conversations, pending approvals)
    GET  /dashboard/memories     → Memories list with search
    GET  /dashboard/approvals    → Pending approvals with approve/reject
    GET  /dashboard/conversations→ Recent conversations with messages
    GET  /dashboard/settings     → Routine status, autonomy mode, quiet hours
    POST /dashboard/approve/{id} → Approve a pending action (HTMX target)
    POST /dashboard/reject/{id}  → Reject a pending action
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# ── auth ──────────────────────────────────────────────────────────────────────

_COOKIE_NAME = "jarvis_dash"


def _dashboard_token() -> str:
    """Return the configured dashboard token (DASHBOARD_TOKEN or TELEGRAM_WEBHOOK_SECRET)."""
    token = (
        getattr(settings, "dashboard_token", "")
        or settings.telegram_webhook_secret
        or ""
    )
    return token.strip()


def _check_auth(request: Request) -> bool:
    token = _dashboard_token()
    if not token:
        return True  # No token configured → open (dev mode)
    # Accept from cookie or query param
    from_cookie = request.cookies.get(_COOKIE_NAME, "")
    from_query = request.query_params.get("token", "")
    return from_cookie == token or from_query == token


def _require_auth(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized — add ?token=<DASHBOARD_TOKEN>")


# ── HTML shell ────────────────────────────────────────────────────────────────

def _page(title: str, body: str, active: str = "") -> HTMLResponse:
    nav_items = [
        ("", "🏠 Overview"),
        ("memories", "🧠 Memórias"),
        ("approvals", "✅ Aprovações"),
        ("conversations", "💬 Conversas"),
        ("settings", "⚙️ Configurações"),
    ]
    nav_html = ""
    for path, label in nav_items:
        href = f"/dashboard/{path}" if path else "/dashboard"
        is_active = "active" if active == path else ""
        nav_html += f'<a href="{href}" class="nav-link {is_active}">{label}</a>\n'

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis — {title}</title>
<style>
  :root {{
    --bg: #0f1117; --surface: #1a1d27; --border: #2d3145;
    --accent: #4f6ef7; --accent2: #7c5cbf; --text: #e2e8f0;
    --muted: #8892a4; --success: #4caf7d; --warn: #f5a623; --danger: #e5534b;
    --radius: 8px; --font: 'Segoe UI', system-ui, sans-serif;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: var(--font); min-height: 100vh; }}
  .layout {{ display: flex; min-height: 100vh; }}
  .sidebar {{
    width: 220px; background: var(--surface); border-right: 1px solid var(--border);
    padding: 24px 0; display: flex; flex-direction: column; flex-shrink: 0;
  }}
  .sidebar-logo {{
    font-size: 1.3rem; font-weight: 700; padding: 0 20px 20px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    border-bottom: 1px solid var(--border); margin-bottom: 12px;
  }}
  .nav-link {{
    display: block; padding: 10px 20px; color: var(--muted);
    text-decoration: none; font-size: 0.9rem; transition: all 0.15s;
    border-left: 3px solid transparent;
  }}
  .nav-link:hover {{ color: var(--text); background: rgba(79,110,247,0.08); }}
  .nav-link.active {{ color: var(--accent); border-left-color: var(--accent); background: rgba(79,110,247,0.1); }}
  .main {{ flex: 1; padding: 32px; overflow: auto; }}
  h1 {{ font-size: 1.5rem; font-weight: 600; margin-bottom: 24px; }}
  h2 {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 16px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.75rem; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; margin-bottom: 20px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 16px; margin-bottom: 28px; }}
  .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; text-align: center; }}
  .stat-num {{ font-size: 2rem; font-weight: 700; color: var(--accent); }}
  .stat-label {{ font-size: 0.75rem; color: var(--muted); margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; padding: 8px 12px; font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border); }}
  td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 0.875rem; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(255,255,255,0.02); }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.7rem; font-weight: 600; }}
  .badge-blue {{ background: rgba(79,110,247,0.2); color: #6b8df7; }}
  .badge-green {{ background: rgba(76,175,125,0.2); color: var(--success); }}
  .badge-warn {{ background: rgba(245,166,35,0.2); color: var(--warn); }}
  .badge-red {{ background: rgba(229,83,75,0.2); color: var(--danger); }}
  .badge-gray {{ background: rgba(136,146,164,0.2); color: var(--muted); }}
  .btn {{ display: inline-block; padding: 6px 14px; border-radius: var(--radius); font-size: 0.8rem; font-weight: 600; cursor: pointer; border: none; transition: opacity 0.15s; text-decoration: none; }}
  .btn-primary {{ background: var(--accent); color: white; }}
  .btn-success {{ background: var(--success); color: white; }}
  .btn-danger {{ background: var(--danger); color: white; }}
  .btn:hover {{ opacity: 0.85; }}
  .search-box {{ width: 100%; padding: 10px 14px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius); color: var(--text); font-size: 0.875rem; margin-bottom: 16px; outline: none; }}
  .search-box:focus {{ border-color: var(--accent); }}
  .empty {{ text-align: center; padding: 40px; color: var(--muted); font-style: italic; }}
  .tag {{ display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 0.7rem; background: var(--border); color: var(--muted); margin-right: 4px; }}
  .msg-user {{ color: var(--text); }}
  .msg-assistant {{ color: #a5b4fc; }}
  .msg-time {{ font-size: 0.7rem; color: var(--muted); }}
  .truncate {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 380px; }}
  .setting-row {{ display: flex; align-items: center; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid var(--border); }}
  .setting-row:last-child {{ border-bottom: none; }}
  .setting-label {{ font-size: 0.9rem; }}
  .setting-sub {{ font-size: 0.75rem; color: var(--muted); margin-top: 2px; }}
  .pill {{ display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 0.75rem; font-weight: 600; }}
  .pill-on {{ background: rgba(76,175,125,0.2); color: var(--success); }}
  .pill-off {{ background: rgba(229,83,75,0.15); color: var(--danger); }}
  .pill-mode {{ background: rgba(79,110,247,0.2); color: var(--accent); }}
</style>
</head>
<body>
<div class="layout">
  <nav class="sidebar">
    <div class="sidebar-logo">⚡ Jarvis</div>
    {nav_html}
  </nav>
  <main class="main">
    <h1>{title}</h1>
    {body}
  </main>
</div>
<script>
  // Simple client-side search for tables
  function filterTable(inputId, tableId) {{
    const input = document.getElementById(inputId);
    const table = document.getElementById(tableId);
    if (!input || !table) return;
    input.addEventListener('input', function() {{
      const q = this.value.toLowerCase();
      table.querySelectorAll('tbody tr').forEach(row => {{
        row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
      }});
    }});
  }}
  filterTable('mem-search', 'mem-table');
  filterTable('conv-search', 'conv-table');
</script>
</body>
</html>""")


# ── data helpers ───────────────────────────────────────────────────────────────

def _safe_json(s: str | None) -> dict:
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    # Convert UTC to configured TZ
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(settings.default_timezone)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(tz)
        return local.strftime("%d/%m %H:%M")
    except Exception:
        return dt.strftime("%d/%m %H:%M")


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s atrás"
    if s < 3600:
        return f"{s // 60}min atrás"
    if s < 86400:
        return f"{s // 3600}h atrás"
    return f"{s // 86400}d atrás"


def _severity_badge(sev: str) -> str:
    mapping = {
        "critical": "badge-red", "high": "badge-warn",
        "medium": "badge-blue", "low": "badge-gray", "info": "badge-gray",
    }
    cls = mapping.get((sev or "").lower(), "badge-gray")
    return f'<span class="badge {cls}">{sev or "?"}</span>'


def _category_badge(cat: str) -> str:
    return f'<span class="tag">{cat}</span>'


# ── routes ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_overview(request: Request, db: Session = Depends(get_db)):
    _require_auth(request)

    from app.models.memory_item import MemoryItem
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.models.pending_approval import PendingApproval
    from app.models.action_log import ActionLog

    user_id = settings.telegram_allowed_user_id or ""

    total_memories = db.query(MemoryItem).filter(
        MemoryItem.user_id == user_id, MemoryItem.is_active == True
    ).count()
    total_convs = db.query(Conversation).filter(Conversation.user_id == user_id).count()
    total_msgs = db.query(Message).filter(Message.user_id == user_id).count()
    pending_approvals = db.query(PendingApproval).filter(
        PendingApproval.user_id == user_id,
        PendingApproval.status == "pending"
    ).count()

    # Recent conversations
    recent_convs = db.query(Conversation).filter(
        Conversation.user_id == user_id
    ).order_by(Conversation.updated_at.desc()).limit(5).all()

    conv_rows = ""
    for c in recent_convs:
        last_msg = db.query(Message).filter(
            Message.conversation_id == c.id,
            Message.role == "user",
        ).order_by(Message.created_at.desc()).first()
        preview = ""
        if last_msg:
            preview = (last_msg.content or "")[:80].replace("<", "&lt;")
        conv_rows += f"""
        <tr>
          <td><a href="/dashboard/conversations?id={c.id}" style="color:var(--accent)">{c.id}</a></td>
          <td class="truncate">{preview}{"…" if len(last_msg.content or "") > 80 else ""}</td>
          <td class="msg-time">{_ago(c.updated_at)}</td>
        </tr>"""

    # Recent action logs
    recent_logs = db.query(ActionLog).order_by(ActionLog.created_at.desc()).limit(8).all()
    log_rows = ""
    for log in recent_logs:
        details = _safe_json(log.details_json)
        log_rows += f"""
        <tr>
          <td class="msg-time">{_fmt_dt(log.created_at)}</td>
          <td>{log.event_type or "—"}</td>
          <td>{'<span class="badge badge-green">ok</span>' if log.status == "success" else f'<span class="badge badge-red">{log.status}</span>'}</td>
          <td class="truncate" style="max-width:300px">{str(details)[:100]}</td>
        </tr>"""

    body = f"""
    <div class="stats">
      <div class="stat"><div class="stat-num">{total_memories}</div><div class="stat-label">Memórias</div></div>
      <div class="stat"><div class="stat-num">{total_convs}</div><div class="stat-label">Conversas</div></div>
      <div class="stat"><div class="stat-num">{total_msgs}</div><div class="stat-label">Mensagens</div></div>
      <div class="stat"><div class="stat-num" style="color:{'var(--warn)' if pending_approvals > 0 else 'var(--success)'}">{pending_approvals}</div><div class="stat-label">Aprovações Pendentes</div></div>
    </div>

    <div class="card">
      <h2>Conversas Recentes</h2>
      <table>
        <thead><tr><th>ID</th><th>Última Mensagem</th><th>Há</th></tr></thead>
        <tbody>{conv_rows or '<tr><td colspan="3" class="empty">Nenhuma conversa</td></tr>'}</tbody>
      </table>
    </div>

    <div class="card">
      <h2>Log de Ações Recentes</h2>
      <table>
        <thead><tr><th>Horário</th><th>Evento</th><th>Status</th><th>Detalhes</th></tr></thead>
        <tbody>{log_rows or '<tr><td colspan="4" class="empty">Sem atividade</td></tr>'}</tbody>
      </table>
    </div>
    """
    return _page("Overview", body, active="")


@router.get("/memories", response_class=HTMLResponse)
async def dashboard_memories(
    request: Request,
    q: str = Query(default=""),
    category: str = Query(default=""),
    db: Session = Depends(get_db),
):
    _require_auth(request)

    from app.models.memory_item import MemoryItem

    user_id = settings.telegram_allowed_user_id or ""

    query = db.query(MemoryItem).filter(
        MemoryItem.user_id == user_id, MemoryItem.is_active == True
    )
    if category:
        query = query.filter(MemoryItem.category == category)
    if q:
        query = query.filter(MemoryItem.content.ilike(f"%{q}%"))

    memories = query.order_by(MemoryItem.created_at.desc()).limit(200).all()

    # Get distinct categories for filter
    cats = db.query(MemoryItem.category).filter(
        MemoryItem.user_id == user_id, MemoryItem.is_active == True
    ).distinct().all()
    cat_options = '<option value="">Todas</option>'
    for (c,) in cats:
        sel = 'selected' if category == c else ''
        cat_options += f'<option value="{c}" {sel}>{c}</option>'

    rows = ""
    for m in memories:
        rows += f"""
        <tr>
          <td>{_category_badge(m.category or "?")}</td>
          <td>{(m.content or "").replace("<", "&lt;")[:300]}</td>
          <td class="msg-time">{m.source or "—"}</td>
          <td class="msg-time">{_ago(m.created_at)}</td>
        </tr>"""

    body = f"""
    <div class="card">
      <form method="get" style="display:flex;gap:12px;margin-bottom:16px">
        <input type="text" name="q" value="{q}" placeholder="Pesquisar memórias…" class="search-box" style="flex:1;margin:0">
        <select name="category" style="padding:10px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-size:0.875rem">
          {cat_options}
        </select>
        <button type="submit" class="btn btn-primary">Filtrar</button>
      </form>
      <p style="color:var(--muted);font-size:0.8rem;margin-bottom:16px">{len(memories)} memórias encontradas</p>
      <table id="mem-table">
        <thead><tr><th>Categoria</th><th>Conteúdo</th><th>Fonte</th><th>Quando</th></tr></thead>
        <tbody>{rows or '<tr><td colspan="4" class="empty">Nenhuma memória encontrada</td></tr>'}</tbody>
      </table>
    </div>
    """
    return _page("Memórias", body, active="memories")


@router.get("/approvals", response_class=HTMLResponse)
async def dashboard_approvals(request: Request, db: Session = Depends(get_db)):
    _require_auth(request)

    from app.models.pending_approval import PendingApproval

    user_id = settings.telegram_allowed_user_id or ""

    all_approvals = db.query(PendingApproval).filter(
        PendingApproval.user_id == user_id
    ).order_by(PendingApproval.created_at.desc()).limit(50).all()

    rows = ""
    for a in all_approvals:
        details = _safe_json(a.details_json)
        action_label = details.get("action", a.action_type or "—")
        status_map = {
            "pending": '<span class="badge badge-warn">pendente</span>',
            "approved": '<span class="badge badge-green">aprovado</span>',
            "rejected": '<span class="badge badge-red">rejeitado</span>',
        }
        status_html = status_map.get(a.status or "", f'<span class="badge badge-gray">{a.status}</span>')
        approve_btn = reject_btn = ""
        if a.status == "pending":
            approve_btn = f'<form method="post" action="/dashboard/approve/{a.id}" style="display:inline"><button class="btn btn-success" type="submit">Aprovar</button></form> '
            reject_btn = f'<form method="post" action="/dashboard/reject/{a.id}" style="display:inline"><button class="btn btn-danger" type="submit">Rejeitar</button></form>'

        rows += f"""
        <tr>
          <td class="msg-time">{a.id}</td>
          <td>{action_label[:60]}</td>
          <td>{status_html}</td>
          <td class="truncate" style="max-width:260px">{str(details.get("params", details))[:120].replace("<","&lt;")}</td>
          <td class="msg-time">{_ago(a.created_at)}</td>
          <td>{approve_btn}{reject_btn}</td>
        </tr>"""

    body = f"""
    <div class="card">
      <table>
        <thead><tr><th>ID</th><th>Ação</th><th>Status</th><th>Detalhes</th><th>Quando</th><th></th></tr></thead>
        <tbody>{rows or '<tr><td colspan="6" class="empty">Nenhuma aprovação registrada</td></tr>'}</tbody>
      </table>
    </div>
    """
    return _page("Aprovações", body, active="approvals")


@router.post("/approve/{approval_id}")
async def dashboard_approve(approval_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request)

    from app.services import approval_service
    user_id = settings.telegram_allowed_user_id or ""
    try:
        approval_service.approve_pending_approval(db, user_id, approval_id)
        await approval_service.execute_approved_action(db, user_id, approval_id)
    except Exception as exc:
        logger.warning("Dashboard approve id=%d: %s", approval_id, exc)
    return RedirectResponse(url="/dashboard/approvals", status_code=303)


@router.post("/reject/{approval_id}")
async def dashboard_reject(approval_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request)

    from app.services import approval_service
    user_id = settings.telegram_allowed_user_id or ""
    try:
        approval_service.reject_pending_approval(db, user_id, approval_id)
    except Exception as exc:
        logger.warning("Dashboard reject id=%d: %s", approval_id, exc)
    return RedirectResponse(url="/dashboard/approvals", status_code=303)


@router.get("/conversations", response_class=HTMLResponse)
async def dashboard_conversations(
    request: Request,
    id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    _require_auth(request)

    from app.models.conversation import Conversation
    from app.models.message import Message

    user_id = settings.telegram_allowed_user_id or ""

    if id:
        # Show messages of a specific conversation
        conv = db.query(Conversation).filter(Conversation.id == id).first()
        if not conv:
            return _page("Conversa não encontrada", '<div class="empty">Conversa não encontrada.</div>', active="conversations")
        messages = db.query(Message).filter(
            Message.conversation_id == id
        ).order_by(Message.created_at.asc()).all()

        msg_html = ""
        for m in messages:
            role_class = "msg-user" if m.role == "user" else "msg-assistant"
            role_label = "👤 Você" if m.role == "user" else "🤖 Jarvis"
            if m.role == "tool":
                role_label = "🔧 Tool"
                role_class = ""
            content = (m.content or "").replace("<", "&lt;").replace("\n", "<br>")[:2000]
            msg_html += f"""
            <div style="margin-bottom:16px;padding:12px;background:var(--bg);border-radius:var(--radius);border-left:3px solid {'var(--accent)' if m.role=='user' else 'var(--accent2)'}">
              <div style="display:flex;justify-content:space-between;margin-bottom:6px">
                <span class="{role_class}" style="font-weight:600;font-size:0.85rem">{role_label}</span>
                <span class="msg-time">{_fmt_dt(m.created_at)}</span>
              </div>
              <div style="font-size:0.875rem;line-height:1.6">{content}</div>
            </div>"""

        body = f"""
        <div style="margin-bottom:16px"><a href="/dashboard/conversations" style="color:var(--accent)">← Todas as conversas</a></div>
        <div class="card" style="padding:0;overflow:hidden">
          <div style="padding:16px;border-bottom:1px solid var(--border);background:var(--surface)">
            <strong>Conversa #{id}</strong>
            <span class="msg-time" style="margin-left:12px">{_fmt_dt(conv.created_at)} → {_fmt_dt(conv.updated_at)}</span>
          </div>
          <div style="padding:16px;max-height:70vh;overflow:auto">{msg_html or '<div class="empty">Sem mensagens</div>'}</div>
        </div>
        """
        return _page(f"Conversa #{id}", body, active="conversations")

    # List all conversations
    convs = db.query(Conversation).filter(
        Conversation.user_id == user_id
    ).order_by(Conversation.updated_at.desc()).limit(100).all()

    rows = ""
    for c in convs:
        msg_count = db.query(Message).filter(Message.conversation_id == c.id).count()
        last = db.query(Message).filter(
            Message.conversation_id == c.id, Message.role == "user"
        ).order_by(Message.created_at.desc()).first()
        preview = ((last.content or "")[:80] if last else "").replace("<", "&lt;")
        rows += f"""
        <tr>
          <td><a href="/dashboard/conversations?id={c.id}" style="color:var(--accent)">#{c.id}</a></td>
          <td class="truncate">{preview}</td>
          <td class="msg-time">{msg_count} msgs</td>
          <td class="msg-time">{_ago(c.updated_at)}</td>
        </tr>"""

    body = f"""
    <div class="card">
      <input type="text" id="conv-search" placeholder="Pesquisar conversas…" class="search-box">
      <table id="conv-table">
        <thead><tr><th>ID</th><th>Última Mensagem</th><th>Msgs</th><th>Atualizada</th></tr></thead>
        <tbody>{rows or '<tr><td colspan="4" class="empty">Nenhuma conversa</td></tr>'}</tbody>
      </table>
    </div>
    """
    return _page("Conversas", body, active="conversations")


@router.get("/settings", response_class=HTMLResponse)
async def dashboard_settings(request: Request, db: Session = Depends(get_db)):
    _require_auth(request)

    from app.models.routine_config import RoutineConfig
    from app.services import autonomy_service
    from app.services import proactive_service

    user_id = settings.telegram_allowed_user_id or ""

    # Routine configs
    configs = db.query(RoutineConfig).filter(RoutineConfig.user_id == user_id).all()
    config_map = {c.routine_type: c.is_enabled for c in configs}

    morning = config_map.get("morning", settings.morning_briefing_enabled)
    midday = config_map.get("midday", settings.midday_checkin_enabled)
    evening = config_map.get("evening", settings.evening_review_enabled)
    reminders_on = config_map.get("reminders", True)
    quiet = proactive_service.get_quiet_hours_preference(db, user_id) and settings.quiet_hours_enabled
    mode = autonomy_service.get_user_autonomy_mode(db, user_id)

    def pill(active: bool) -> str:
        return f'<span class="pill pill-on">✅ ativo</span>' if active else f'<span class="pill pill-off">❌ off</span>'

    body = f"""
    <div class="card">
      <h2>Rotinas Proativas</h2>
      <div class="setting-row">
        <div><div class="setting-label">☀️ Briefing Matinal</div><div class="setting-sub">{settings.morning_briefing_time}</div></div>
        {pill(morning)}
      </div>
      <div class="setting-row">
        <div><div class="setting-label">🕐 Checkpoint Meio-dia</div><div class="setting-sub">{settings.midday_checkin_time}</div></div>
        {pill(midday)}
      </div>
      <div class="setting-row">
        <div><div class="setting-label">🌙 Fechamento do Dia</div><div class="setting-sub">{settings.evening_review_time}</div></div>
        {pill(evening)}
      </div>
      <div class="setting-row">
        <div><div class="setting-label">🔔 Lembretes</div><div class="setting-sub">eventos, tarefas, follow-ups</div></div>
        {pill(reminders_on)}
      </div>
    </div>

    <div class="card">
      <h2>Preferências</h2>
      <div class="setting-row">
        <div><div class="setting-label">🌙 Quiet Hours</div><div class="setting-sub">{settings.quiet_hours_start} – {settings.quiet_hours_end}</div></div>
        {pill(quiet)}
      </div>
      <div class="setting-row">
        <div><div class="setting-label">🤖 Modo de Autonomia</div><div class="setting-sub">conservative / hybrid_safe / aggressive</div></div>
        <span class="pill pill-mode">{mode}</span>
      </div>
      <div class="setting-row">
        <div><div class="setting-label">🔊 Respostas por Áudio</div><div class="setting-sub">VOICE_RESPONSES_ENABLED</div></div>
        {pill(settings.voice_responses_enabled)}
      </div>
      <div class="setting-row">
        <div><div class="setting-label">🌐 Browser Automation</div><div class="setting-sub">Playwright (supervisionado)</div></div>
        {pill(settings.browser_automation_enabled)}
      </div>
      <div class="setting-row">
        <div><div class="setting-label">🔗 Cross-App Enrichment</div><div class="setting-sub">briefing pré-reunião</div></div>
        {pill(getattr(settings, 'cross_app_enrichment_enabled', False))}
      </div>
    </div>

    <div class="card">
      <h2>Informações do Sistema</h2>
      <div class="setting-row">
        <div class="setting-label">Modelo LLM</div>
        <span class="tag">{settings.openai_model}</span>
      </div>
      <div class="setting-row">
        <div class="setting-label">Fuso Horário</div>
        <span class="tag">{settings.default_timezone}</span>
      </div>
      <div class="setting-row">
        <div class="setting-label">Proatividade Global</div>
        {pill(settings.proactive_features_enabled)}
      </div>
      <div class="setting-row">
        <div class="setting-label">Ambiente</div>
        <span class="tag">{settings.app_env}</span>
      </div>
    </div>

    <div style="color:var(--muted);font-size:0.8rem;margin-top:8px">
      Para alterar configurações, use os comandos no Telegram: /autonomy, /routineon, /routineoff, /quieton, /voiceon
    </div>
    """
    return _page("Configurações", body, active="settings")
