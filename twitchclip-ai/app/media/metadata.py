"""Kurze Videoüberschrift, TikTok-Beschreibung und höchstens fünf relevante Hashtags.

Regelbasiert und nachvollziehbar (keine externe KI nötig): Titel aus dem Zuschauer-Clip-Titel oder dem
aufregendsten Satz des Transkripts, Beschreibung mit Quellenangabe des Streamers, Hashtags aus Kanal,
Spiel und Plattform. Alles bleibt vor der Freigabe im Dashboard editierbar.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_HASHTAGS = 5
MAX_TITLE = 60
TIKTOK_CAPTION_LIMIT = 2200
GENERIC_TITLES = ("highlight", "chat-highlight", "beispiel", "clip", "stream")

TEXTS = {
    "de": {
        "fallback": "Krasser Moment bei {name}",
        "source": "Live auf Twitch: twitch.tv/{login}",
        "game": "🎮 {game}",
        "sample": "Testvideo (TwitchClip AI Testmodus)",
    },
    "en": {
        "fallback": "Insane moment from {name}",
        "source": "Live on Twitch: twitch.tv/{login}",
        "game": "🎮 {game}",
        "sample": "Test video (TwitchClip AI test mode)",
    },
}
LANGUAGE_TAGS = {"de": "twitchdeutschland", "en": "twitchclips"}


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    description: str
    hashtags: list[str]


def hashtag(value: str) -> str:
    """'Counter-Strike 2' → 'counterstrike2' (nur Buchstaben/Ziffern, keine Umlaute-Probleme)."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", normalized.lower())[:30]


def _is_generic(title: str | None) -> bool:
    if not title or len(title.strip()) < 4:
        return True
    lowered = title.lower()
    return any(lowered.endswith(g) or lowered.startswith("beispiel-") for g in GENERIC_TITLES)


def _shorten(text: str, limit: int = MAX_TITLE) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:-–") + "…"


def _quote_from_transcript(words: list[dict]) -> str | None:
    """Die kürzeste aufregende Passage (Ausruf/Schlüsselwort) als Zitat."""
    if not words:
        return None
    tokens = [w["text"].strip() for w in words if w.get("text", "").strip()]
    for i, token in enumerate(tokens):
        if "!" in token or token.isupper() and len(token) > 2:
            quote = " ".join(tokens[max(0, i - 3) : i + 1])
            return quote.strip(" ,")
    return None


def build_metadata(
    *,
    streamer_name: str,
    streamer_login: str,
    language: str,
    source_title: str | None,
    words: list[dict] | None,
    game: str | None,
    is_sample: bool = False,
) -> VideoMetadata:
    lang = language if language in TEXTS else "de"
    texts = TEXTS[lang]
    if not _is_generic(source_title):
        title = _shorten(source_title)
    else:
        quote = _quote_from_transcript(words or [])
        title = (
            _shorten(f"„{quote}“ – {streamer_name}")
            if quote
            else texts["fallback"].format(name=streamer_name)
        )

    tags: list[str] = []
    for candidate in (
        hashtag(streamer_login),
        hashtag(game) if game else "",
        "twitch",
        "gaming",
        LANGUAGE_TAGS.get(lang, "twitchclips"),
    ):
        if candidate and candidate not in tags:
            tags.append(candidate)
    tags = tags[:MAX_HASHTAGS]

    lines = [title]
    if game:
        lines.append(texts["game"].format(game=game))
    lines.append(texts["sample"] if is_sample else texts["source"].format(login=streamer_login))
    description = "\n".join(lines)
    return VideoMetadata(title=title, description=description, hashtags=tags)


def caption_text(description: str, hashtags: list[str]) -> str:
    """Text für das TikTok-Feld „title“ (Beschreibung + Hashtags), auf das API-Limit gekürzt."""
    tags = " ".join(f"#{t}" for t in hashtags[:MAX_HASHTAGS])
    text = f"{description.strip()}\n\n{tags}".strip()
    return text[:TIKTOK_CAPTION_LIMIT]


def normalize_hashtags(raw: str | list[str]) -> list[str]:
    """Eingabe aus dem Dashboard ('#a, b #c') → bereinigte Liste, max. fünf, ohne Duplikate."""
    items = raw if isinstance(raw, list) else re.split(r"[\s,;]+", raw or "")
    result: list[str] = []
    for item in items:
        tag = hashtag(item.lstrip("#"))
        if tag and tag not in result:
            result.append(tag)
    return result[:MAX_HASHTAGS]
