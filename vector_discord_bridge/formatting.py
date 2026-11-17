"""Text conversion between Discord and Vector messages."""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

DISCORD_LIMIT = 2000

_CUSTOM_EMOJI = re.compile(r"<a?:(?P<name>[A-Za-z0-9_~]+):\d+>")
_NPUB = re.compile(r"(?:nostr:)?(npub1[0-9a-z]{58})")
_SHORTCODE = re.compile(r"(?<![<\w]):([A-Za-z0-9_~]{2,32}):(?!\d)")
_EVERYONE = re.compile(r"@(everyone|here)")
_BANNED_WEBHOOK_WORDS = re.compile(r"discord|clyde", re.IGNORECASE)


def discord_emoji_to_shortcodes(text: str) -> str:
    """``<:wave:123>`` / ``<a:wave:123>`` → ``:wave:`` (Vector shows these from equipped emoji packs)."""
    return _CUSTOM_EMOJI.sub(lambda m: f":{m.group('name')}:", text)


def vector_header(author_name: str, text: str, *, thread: Optional[str] = None, stamp: Optional[int] = None) -> str:
    """Vector has no per-message display name, so a relayed message starts with a bold name line."""
    head = f"**{author_name}**"
    if thread:
        head += f" · 🧵 {thread}"
    if stamp:
        head += f" · <t:{stamp}:f>"
    return f"{head}\n{text}" if text else head


def to_vector(content: str, author_name: str, *, thread: Optional[str] = None, quote: Optional[str] = None,
              extras: Optional[List[str]] = None, stamp: Optional[int] = None) -> str:
    """Build the Vector body for a Discord message. ``content`` should be ``message.clean_content``."""
    body = discord_emoji_to_shortcodes(content or "")
    parts = [quote] if quote else []
    if body:
        parts.append(body)
    parts.extend(extras or [])
    return vector_header(author_name, "\n".join(parts), thread=thread, stamp=stamp)


def to_discord(content: str, *, resolve_npub: Callable[[str], Optional[str]] = lambda n: None,
               emojis: Optional[Dict[str, str]] = None) -> str:
    """Build the Discord body for a Vector message.

    * ``nostr:npub1…`` mentions become ``@name`` (or a short npub) so they don't render as raw keys
    * ``:shortcode:`` becomes the guild's ``<:shortcode:id>`` when the Discord server has that emoji
    * ``@everyone`` / ``@here`` are defused (also enforced via ``AllowedMentions.none()``)
    """
    def npub(m: "re.Match[str]") -> str:
        name = resolve_npub(m.group(1))
        return f"@{name}" if name else f"@{m.group(1)[:12]}…"

    text = _NPUB.sub(npub, content or "")
    if emojis:
        text = _SHORTCODE.sub(lambda m: emojis.get(m.group(1), m.group(0)), text)
    return _EVERYONE.sub("@​\\1", text)


def reply_quote(author: Optional[str], snippet: Optional[str], *, limit: int = 80) -> str:
    """A one-line quote used on Discord where webhooks can't make native replies."""
    snippet = (snippet or "").replace("\n", " ").strip()
    if len(snippet) > limit:
        snippet = snippet[: limit - 1] + "…"
    who = f"**{author}**: " if author else ""
    return f"> ↪ {who}{snippet}" if (who or snippet) else ""


def webhook_username(name: str, suffix: str = "") -> str:
    """Discord webhook names: 1–80 chars and may not contain 'discord' or 'clyde'."""
    name = _BANNED_WEBHOOK_WORDS.sub(lambda m: m.group(0)[0] + "​" + m.group(0)[1:], name.strip() or "Vector user")
    full = f"{name}{suffix}"
    return full[:80]


def split_discord(text: str, limit: int = DISCORD_LIMIT) -> List[str]:
    """Split text into Discord-sized chunks, preferring line breaks."""
    if len(text) <= limit:
        return [text]
    out: List[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out
