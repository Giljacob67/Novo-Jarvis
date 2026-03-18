import json
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.conversation_state import ConversationState


GENERIC_REFERENCES = {
    "isso",
    "resolve isso",
    "faça isso",
    "faz isso",
    "agende isso",
    "agende esse prazo",
    "organiza isso",
}


def _infer_intent(text: str) -> tuple[str, float]:
    t = (text or "").lower()
    if any(k in t for k in ("email", "inbox", "rascunho", "gmail")):
        return "email", 0.86
    if any(k in t for k in ("agenda", "evento", "reunião", "reuniao", "calend")):
        return "calendar", 0.84
    if any(k in t for k in ("tarefa", "prazo", "task", "follow-up", "followup")):
        return "tasks", 0.86
    if any(k in t for k in ("drive", "arquivo", "documento")):
        return "drive", 0.8
    if any(k in t for k in ("notícia", "noticias", "news", "internet", "google", "manchetes", "headlines")):
        return "web_research", 0.8
    if any(k in t for k in ("resolve isso", "faça isso", "faz isso")):
        return "goal_execution", 0.75
    return "general", 0.55


def _infer_entity(text: str) -> str | None:
    if not text:
        return None
    proc = re.search(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}", text)
    if proc:
        return proc.group(0)
    date_ref = re.search(r"\b\d{2}/\d{2}/\d{4}\b", text)
    if date_ref:
        return date_ref.group(0)
    quoted = re.search(r'"([^"]{4,120})"', text)
    if quoted:
        return quoted.group(1)
    return None


def get_state(db: Session, user_id: str) -> ConversationState | None:
    return (
        db.query(ConversationState)
        .filter(ConversationState.user_id == user_id)
        .first()
    )


def upsert_state(
    db: Session,
    user_id: str,
    last_intent: str,
    confidence: float,
    last_entity: str | None = None,
    pending_action: str | None = None,
) -> ConversationState:
    state = get_state(db, user_id)
    if state is None:
        state = ConversationState(
            user_id=user_id,
            last_intent=last_intent,
            confidence=confidence,
            last_entity=last_entity,
            pending_action=pending_action,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(state)
        db.commit()
        db.refresh(state)
        return state

    state.last_intent = last_intent
    state.confidence = confidence
    if last_entity:
        state.last_entity = last_entity
    if pending_action is not None:
        state.pending_action = pending_action
    state.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(state)
    return state


def update_state_from_text(db: Session, user_id: str, text: str) -> ConversationState:
    intent, confidence = _infer_intent(text)
    entity = _infer_entity(text)
    pending_action = None
    if intent in {"goal_execution", "tasks", "calendar"}:
        pending_action = "execution_or_confirmation"
    return upsert_state(
        db,
        user_id,
        last_intent=intent,
        confidence=confidence,
        last_entity=entity,
        pending_action=pending_action,
    )


def augment_text_with_state(db: Session, user_id: str, text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return raw

    raw_lower = raw.lower()
    if raw_lower not in GENERIC_REFERENCES:
        return raw

    state = get_state(db, user_id)
    if state is None:
        return raw

    context_chunks: list[str] = []
    if state.last_entity:
        context_chunks.append(f"entidade: {state.last_entity}")
    if state.last_intent:
        context_chunks.append(f"intenção anterior: {state.last_intent}")
    if state.pending_action:
        context_chunks.append(f"pendência: {state.pending_action}")
    if not context_chunks:
        return raw

    return f"{raw}\n\n[CONTEXTO_RECUPERADO]\n" + " | ".join(context_chunks) + "\n[/CONTEXTO_RECUPERADO]"


# ── Multi-turn: pending questions ────────────────────────────────────────────

_QUESTION_PATTERNS = re.compile(
    r"(\?|quer que eu|posso ajudar|você gostaria|confirma|me diga|"
    r"pode me informar|qual é|quando é|como você prefere)\s*$",
    re.IGNORECASE,
)


def detect_pending_question(reply: str) -> Optional[str]:
    """Extract the last sentence from reply if it contains a question.
    Returns None if the reply does not end with an open question."""
    if not reply:
        return None
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", reply.strip()) if s.strip()]
    if not sentences:
        return None
    last = sentences[-1]
    if _QUESTION_PATTERNS.search(last) or last.endswith("?"):
        return last
    return None


def set_pending_question(db: Session, user_id: str, question: Optional[str]) -> None:
    """Persist (or clear) the last open question Jarvis asked."""
    state = get_state(db, user_id)
    if state is None:
        return
    state.pending_question = question
    state.updated_at = datetime.now(timezone.utc)
    db.commit()


def get_pending_question(db: Session, user_id: str) -> Optional[str]:
    state = get_state(db, user_id)
    return state.pending_question if state else None


# ── Multi-turn: user commitments ─────────────────────────────────────────────

_COMMITMENT_PATTERNS = re.compile(
    r"\b(vou |farei |vou fazer |amanhã faço|vou enviar|vou mandar|vou agendar|"
    r"vou ligar|vou verificar|vou resolver|farei isso|confirmo|confirmarei|"
    r"vou tratar|vou checar)\b",
    re.IGNORECASE,
)


def detect_commitment(text: str) -> Optional[str]:
    """Return the user text if it looks like a commitment; else None."""
    if not text:
        return None
    if _COMMITMENT_PATTERNS.search(text):
        return text[:200]  # store first 200 chars
    return None


def add_commitment(db: Session, user_id: str, text: str) -> None:
    """Append a new commitment to the user's open commitments list."""
    state = get_state(db, user_id)
    if state is None:
        return
    existing: list[dict] = []
    if state.open_commitments_json:
        try:
            existing = json.loads(state.open_commitments_json)
        except Exception:
            existing = []
    existing.append({
        "text": text,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolved": False,
    })
    # Keep at most 10 most recent commitments
    state.open_commitments_json = json.dumps(existing[-10:], ensure_ascii=False)
    state.updated_at = datetime.now(timezone.utc)
    db.commit()


def get_open_commitments(db: Session, user_id: str) -> list[str]:
    """Return unresolved commitment texts."""
    state = get_state(db, user_id)
    if not state or not state.open_commitments_json:
        return []
    try:
        items = json.loads(state.open_commitments_json)
        return [i["text"] for i in items if not i.get("resolved")]
    except Exception:
        return []


def build_multiturn_context(db: Session, user_id: str) -> str:
    """Format pending question + open commitments as an injection string for the LLM."""
    parts: list[str] = []
    question = get_pending_question(db, user_id)
    if question:
        parts.append(f"Pergunta aberta que você fez ao usuário (ele pode estar respondendo agora): {question}")
    commitments = get_open_commitments(db, user_id)
    if commitments:
        clist = "\n".join(f"  • {c}" for c in commitments[:5])
        parts.append(f"Compromissos pendentes do usuário (mencionados em sessões anteriores):\n{clist}")
    return "\n\n".join(parts)
