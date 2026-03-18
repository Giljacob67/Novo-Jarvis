"""User Profile Service — builds and maintains a structured personal context card.

The profile is accumulated over time via background LLM extraction after each
conversation turn. It is injected into the system prompt so Jarvis always
"knows" who the user is, what they're working on and who they work with.

Profile JSON schema (all fields optional):
{
  "name": "Gilberto Jacob",
  "company": "Jacob Advogados",
  "role": "Advogado / Gestor",
  "projects": [{"name": "Projeto Alpha", "status": "ativo"}],
  "contacts": [{"name": "João", "role": "cliente", "company": "Empresa X"}],
  "preferences": ["Prefere respostas objetivas e diretas"],
  "patterns": {"typical_start": "08:00", "focus_areas": ["direito"]}
}
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.user_profile import UserProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read / Write
# ---------------------------------------------------------------------------

def get_profile(db: Session, user_id: str) -> dict:
    """Return the stored profile dict, or {} if none exists yet."""
    record = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not record:
        return {}
    try:
        return json.loads(record.profile_json)
    except Exception:
        return {}


def upsert_profile(db: Session, user_id: str, updates: dict) -> None:
    """Deep-merge *updates* into the existing profile and persist.

    Lists (contacts, projects, preferences) are appended with deduplication.
    Scalar fields are replaced only when the new value is non-empty.
    No-op when *updates* is empty.
    """
    if not updates:
        return

    record = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if record:
        try:
            existing = json.loads(record.profile_json)
        except Exception:
            existing = {}
    else:
        existing = {}
        record = UserProfile(user_id=user_id)
        db.add(record)

    merged = _deep_merge(existing, updates)
    record.profile_json = json.dumps(merged, ensure_ascii=False)
    record.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.debug("Profile upserted for user=%s keys=%s", user_id, list(updates.keys()))


# ---------------------------------------------------------------------------
# Deep merge helpers
# ---------------------------------------------------------------------------

def _item_key(item: Any) -> str:
    """Stable key for deduplication of list entries."""
    if isinstance(item, dict):
        return (item.get("name") or item.get("content") or json.dumps(item, sort_keys=True)).lower()
    return str(item).lower()


def _deep_merge(base: dict, updates: dict) -> dict:
    """Recursive merge — lists grow, scalars are replaced, dicts recurse."""
    result = dict(base)
    for key, value in updates.items():
        # Skip empty / null values
        if value is None or value == "" or value == [] or value == {}:
            continue

        existing = result.get(key)

        if isinstance(value, list) and isinstance(existing, list):
            # Append items not already present (by name/content key)
            seen = {_item_key(i) for i in existing}
            for item in value:
                k = _item_key(item)
                if k not in seen:
                    existing.append(item)
                    seen.add(k)
            result[key] = existing
        elif isinstance(value, dict) and isinstance(existing, dict):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = value

    return result


# ---------------------------------------------------------------------------
# Format for system prompt injection
# ---------------------------------------------------------------------------

def format_profile_context(profile: dict) -> str:
    """Render the profile as a concise section for injection into the system prompt.

    Returns an empty string when the profile is empty or has too little data
    to be useful (avoids cluttering the prompt with a near-empty block).
    """
    if not profile:
        return ""

    lines: list[str] = []

    identity_parts: list[str] = []
    if profile.get("name"):
        identity_parts.append(f"Nome: {profile['name']}")
    if profile.get("role"):
        identity_parts.append(f"Cargo: {profile['role']}")
    if profile.get("company"):
        identity_parts.append(f"Empresa: {profile['company']}")
    if identity_parts:
        lines.append("**Identidade:** " + " | ".join(identity_parts))

    projects = profile.get("projects", [])
    if projects:
        names = [
            p.get("name") if isinstance(p, dict) else str(p)
            for p in projects[:6]
        ]
        lines.append(f"**Projetos ativos:** {', '.join(names)}")

    contacts = profile.get("contacts", [])
    if contacts:
        contact_parts: list[str] = []
        for c in contacts[:6]:
            if isinstance(c, dict) and c.get("name"):
                name = c["name"]
                role = c.get("role", "")
                company = c.get("company", "")
                detail = name
                extras = [r for r in [role, company] if r]
                if extras:
                    detail += f" ({', '.join(extras)})"
                contact_parts.append(detail)
        if contact_parts:
            lines.append(f"**Contatos chave:** {'; '.join(contact_parts)}")

    preferences = profile.get("preferences", [])
    if preferences:
        lines.append(f"**Preferências:** {'; '.join(str(p) for p in preferences[:4])}")

    patterns = profile.get("patterns", {})
    pattern_parts: list[str] = []
    if patterns.get("typical_start"):
        pattern_parts.append(f"inicia o dia ~{patterns['typical_start']}")
    if patterns.get("focus_areas"):
        areas = ", ".join(patterns["focus_areas"][:3])
        pattern_parts.append(f"foco em {areas}")
    if pattern_parts:
        lines.append(f"**Padrões:** {'; '.join(pattern_parts)}")

    if not lines:
        return ""

    header = "## O que você sabe sobre o usuário"
    return header + "\n" + "\n".join(lines)
