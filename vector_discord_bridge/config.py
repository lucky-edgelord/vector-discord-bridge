"""Settings, read from the environment (and a ``.env`` file if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=value lines); existing environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _list(name: str) -> List[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


@dataclass
class Settings:
    discord_token: str = ""
    vector_nsec: Optional[str] = None
    discord_guild_id: Optional[int] = None
    vector_community_id: Optional[str] = None
    db_path: str = "bridge.db"
    vector_data_dir: str = "./vector-bot-data"
    relays: List[str] = field(default_factory=list)

    # clone
    clone_history: int = 0            # messages per channel to copy (0 = none)
    hidden_channels: str = "private"  # private | overwrite | skip
    clone_delay: float = 0.5          # seconds between Vector structure changes

    # bridge
    sync_structure: bool = True       # mirror Discord channel/role changes to Vector live
    relay_bots: bool = True           # relay messages from other Discord bots
    use_webhooks: bool = True         # show Vector users with their own name/avatar on Discord
    name_suffix: str = " (Vector)"    # appended to Vector users' names on Discord
    max_attachment_mb: int = 25
    discord_invite_url: Optional[str] = None
    prune_days: int = 90
    catch_up_hours: int = 24          # relay Vector messages missed while offline, up to this age
    emoji_pack: Optional[str] = None  # naddr of a Vector emoji pack holding the server's emoji

    @classmethod
    def from_env(cls, dotenv: str = ".env") -> "Settings":
        load_dotenv(dotenv)
        guild = os.environ.get("DISCORD_GUILD_ID")
        return cls(
            discord_token=os.environ.get("DISCORD_TOKEN", ""),
            vector_nsec=os.environ.get("VECTOR_NSEC") or None,
            discord_guild_id=int(guild) if guild else None,
            vector_community_id=os.environ.get("VECTOR_COMMUNITY_ID") or None,
            db_path=os.environ.get("BRIDGE_DB", "bridge.db"),
            vector_data_dir=os.environ.get("VECTOR_DATA_DIR", "./vector-bot-data"),
            relays=_list("VECTOR_RELAYS"),
            clone_history=_int("CLONE_HISTORY", 0),
            hidden_channels=os.environ.get("HIDDEN_CHANNELS", "private").lower(),
            clone_delay=float(os.environ.get("CLONE_DELAY", "0.5")),
            sync_structure=_bool("SYNC_STRUCTURE", True),
            relay_bots=_bool("RELAY_BOTS", True),
            use_webhooks=_bool("USE_WEBHOOKS", True),
            name_suffix=os.environ.get("NAME_SUFFIX", " (Vector)"),
            max_attachment_mb=_int("MAX_ATTACHMENT_MB", 25),
            discord_invite_url=os.environ.get("DISCORD_INVITE_URL") or None,
            prune_days=_int("PRUNE_DAYS", 90),
            catch_up_hours=_int("CATCH_UP_HOURS", 24),
            emoji_pack=os.environ.get("VECTOR_EMOJI_PACK") or None,
        )
