from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import httpx


logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS_SEARCH = "https://news.google.com/rss/search"
DEFAULT_TOPICS = ["tecnologia", "ia", "brasil", "mundo"]
TOPIC_LABELS = {
    "tecnologia": "Tecnologia",
    "ia": "Inteligência Artificial",
    "brasil": "Brasil",
    "mundo": "Mundo",
}
TOPIC_QUERIES = {
    "tecnologia": "tecnologia OR startups OR inovação digital",
    "ia": "\"inteligência artificial\" OR IA OR OpenAI OR modelos de linguagem",
    "brasil": "Brasil política economia justiça",
    "mundo": "mundo internacional geopolítica economia global",
}


def _build_google_news_search_url(query: str) -> str:
    q = quote_plus(query)
    return f"{GOOGLE_NEWS_RSS_SEARCH}?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"


def _parse_topics_from_text(user_text: str) -> list[str]:
    t = (user_text or "").lower()
    selected: list[str] = []

    if any(k in t for k in ("tecnologia", "tech", "startup", "inovação", "inovacao")):
        selected.append("tecnologia")
    if any(k in t for k in ("inteligência artificial", "inteligencia artificial", "ia", "llm", "openai")):
        selected.append("ia")
    if any(k in t for k in ("brasil", "nacional", "brasileiras")):
        selected.append("brasil")
    if any(k in t for k in ("mundo", "internacional", "global", "exterior")):
        selected.append("mundo")

    if selected:
        return list(dict.fromkeys(selected))
    return DEFAULT_TOPICS.copy()


def _safe_text(raw: str, max_len: int = 160) -> str:
    text = (raw or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _extract_rss_items(xml_text: str, limit: int = 3) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    output: list[dict[str, str]] = []
    seen_titles: set[str] = set()

    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None and source_el.text else ""
        if not title:
            continue
        title_key = title.lower().strip()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        output.append(
            {
                "title": _safe_text(title, 180),
                "link": link,
                "source": _safe_text(source, 60) if source else "",
                "published_at": pub,
            }
        )
        if len(output) >= limit:
            break
    return output


async def _fetch_topic_headlines(topic: str, per_topic: int = 3) -> list[dict[str, str]]:
    query = TOPIC_QUERIES.get(topic)
    if not query:
        return []
    url = _build_google_news_search_url(query)

    headers = {"User-Agent": "Jarvis-News/1.0 (+https://github.com/Giljacob67/Novo-Jarvis)"}
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return _extract_rss_items(resp.text, limit=per_topic)


async def get_automatic_headlines_payload(user_text: str = "", per_topic: int = 3) -> dict[str, Any]:
    topics = _parse_topics_from_text(user_text)
    result: dict[str, list[dict[str, str]]] = {}
    failures: dict[str, str] = {}

    for topic in topics:
        try:
            result[topic] = await _fetch_topic_headlines(topic, per_topic=per_topic)
        except Exception as e:
            logger.exception("Failed to fetch headlines for topic=%s", topic)
            failures[topic] = str(e)
            result[topic] = []

    return {
        "topics": topics,
        "items": result,
        "failures": failures,
        "generated_at": datetime.utcnow().isoformat(),
    }


def compose_automatic_headlines(payload: dict[str, Any]) -> str:
    lines = ["🗞️ *Manchetes automáticas (agora)*", ""]
    topics = payload.get("topics", [])
    items = payload.get("items", {})

    has_any = False
    for topic in topics:
        label = TOPIC_LABELS.get(topic, topic.title())
        lines.append(f"*{label}*")
        topic_items = items.get(topic, [])
        if not topic_items:
            lines.append("• Sem destaques no momento.")
            lines.append("")
            continue
        has_any = True
        for idx, row in enumerate(topic_items, 1):
            source = row.get("source") or "fonte não informada"
            lines.append(f"{idx}. {row.get('title', '(sem título)')}")
            lines.append(f"   Fonte: {source}")
            if row.get("link"):
                lines.append(f"   🔗 {row['link']}")
        lines.append("")

    if not has_any:
        lines.append("Não consegui coletar manchetes agora. Tente novamente em instantes.")
        return "\n".join(lines)

    lines.append("*Atalhos*")
    lines.append("• /headlines ia")
    lines.append("• /headlines tecnologia")
    lines.append("• /headlines brasil")
    lines.append("• /headlines mundo")
    return "\n".join(lines)


async def get_automatic_headlines_brief(user_text: str = "", per_topic: int = 3) -> str:
    payload = await get_automatic_headlines_payload(user_text=user_text, per_topic=per_topic)
    return compose_automatic_headlines(payload)
