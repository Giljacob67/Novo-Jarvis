from datetime import datetime, timezone

from sqlalchemy import Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UserProfile(Base):
    """Stores a structured JSON profile for each user.

    Accumulated over time via background extraction after every conversation
    turn. Used to inject personal context into the system prompt so Jarvis
    truly "knows" the user across sessions.

    The profile_json field is a JSON blob with the following schema (all
    fields optional):
    {
      "name": "Gilberto Jacob",
      "company": "Jacob Advogados",
      "role": "Advogado / Gestor",
      "projects": [{"name": "Projeto Alpha", "status": "ativo"}],
      "contacts": [{"name": "João", "role": "cliente", "company": "Empresa X"}],
      "preferences": ["Prefere respostas objetivas"],
      "patterns": {"typical_start": "08:00", "focus_areas": ["direito"]}
    }
    """

    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    profile_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
