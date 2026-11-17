"""Small in-memory stand-ins for the Vector and Discord objects the bridge touches."""

from __future__ import annotations

import datetime
import itertools
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import discord

from vector_discord_bridge.config import Settings

_ids = itertools.count(1)
BOT_NPUB = "npub1" + "b" * 58
ALICE_NPUB = "npub1" + "a" * 58


def vid() -> str:
    return f"{next(_ids):064x}"


def snowflake() -> int:
    return 10**17 + next(_ids)


# --------------------------------------------------------------------------------------------
# Vector
# --------------------------------------------------------------------------------------------


class VMessage:
    def __init__(self, channel: "VChannel", content: str, *, author: Any = None, mine: bool = False,
                 mid: Optional[str] = None, reference: Any = None, attachments: Optional[list] = None):
        self.id = mid or vid()
        self.channel = channel
        self.community = channel.community
        self.content = content
        self.mine = mine
        self.author = author or SimpleNamespace(npub=BOT_NPUB, display_name="bridge", avatar=None)
        self.reference = reference
        self.attachments = attachments or []
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True
        self.channel.deleted.append(self.id)


class VPartial:
    def __init__(self, channel: "VChannel", mid: str):
        self.channel, self.id = channel, mid

    async def edit(self, content: str = None, **kw: Any) -> None:
        self.channel.edits.append((self.id, content))
        if self.id in self.channel.messages:
            self.channel.messages[self.id].content = content

    async def delete(self) -> None:
        self.channel.deleted.append(self.id)


class VChannel:
    def __init__(self, community: "VCommunity", name: str, view: str = "chat", *, category_id: Optional[str] = None,
                 private: bool = False, topic: Optional[str] = None):
        self.id = vid()
        self.community = community
        self.name = name
        self.view = view
        self.category_id = category_id
        self.private = private
        self.topic = topic
        self.sent: List[Dict[str, Any]] = []
        self.messages: Dict[str, VMessage] = {}
        self.edits: List[tuple] = []
        self.deleted: List[str] = []
        self.overwrites: Dict[Any, Any] = {}
        self.channel_edits: List[Dict[str, Any]] = []

    async def send(self, content: Any = None, **kw: Any) -> VMessage:
        m = VMessage(self, content or "", mine=True)
        self.messages[m.id] = m
        self.sent.append({"content": content, **kw, "id": m.id})
        return m

    async def fetch_message(self, mid: str) -> VMessage:
        if mid not in self.messages:
            raise LookupError(mid)
        return self.messages[mid]

    def get_partial_message(self, mid: str) -> VPartial:
        return VPartial(self, mid)

    async def set_permissions(self, target: Any, *, overwrite: Any) -> None:
        self.overwrites[target] = overwrite

    async def edit(self, **kw: Any) -> "VChannel":
        self.channel_edits.append(kw)
        for k, v in kw.items():
            if k == "category":
                self.category_id = v.id if v else None
            elif hasattr(self, k):
                setattr(self, k, v)
        return self


class VCategory:
    def __init__(self, name: str):
        self.id = vid()
        self.name = name
        self.overwrites: Dict[Any, Any] = {}

    async def set_permissions(self, target: Any, *, overwrite: Any) -> None:
        self.overwrites[target] = overwrite

    async def edit(self, **kw: Any) -> None:
        self.name = kw.get("name", self.name)

    async def delete(self) -> None:
        self.deleted = True


class VRole:
    def __init__(self, name: str, permissions: int, colour: int, hoist: bool, mentionable: bool):
        self.id = vid()
        self.name, self.hoist, self.mentionable = name, hoist, mentionable
        self.permissions = permissions
        self.colour = SimpleNamespace(value=colour)
        self.position = 0
        self.deleted = False

    async def edit(self, **kw: Any) -> None:
        self.name = kw["name"]
        self.permissions = kw["permissions"]

    async def delete(self) -> None:
        self.deleted = True


class VCommunity:
    def __init__(self, name: str = "Vec"):
        self.id = vid()
        self.name = name
        self.description: Optional[str] = None
        self.default_role = SimpleNamespace(id="everyone", name="@everyone")
        self._channels: Dict[str, VChannel] = {}
        self._categories: Dict[str, VCategory] = {}
        self._roles: Dict[str, VRole] = {}
        self.everyone_perms = 0
        self.edits: List[Dict[str, Any]] = []
        self.role_positions: Dict[Any, int] = {}

    @property
    def channels(self) -> List[VChannel]:
        return list(self._channels.values())

    def get_channel(self, cid: str) -> Optional[VChannel]:
        return self._channels.get(cid)

    def get_category(self, cid: str) -> Optional[VCategory]:
        return self._categories.get(cid)

    def get_role(self, rid: str) -> Optional[VRole]:
        return self._roles.get(rid)

    async def fetch_channels(self) -> List[VChannel]:
        return self.channels

    async def edit(self, **kw: Any) -> None:
        self.edits.append(kw)
        self.name = kw.get("name", self.name)

    async def set_default_permissions(self, perms: int) -> None:
        self.everyone_perms = perms

    async def create_role(self, **kw: Any) -> VRole:
        r = VRole(kw["name"], kw["permissions"], kw["colour"], kw["hoist"], kw["mentionable"])
        self._roles[r.id] = r
        return r

    async def edit_role_positions(self, positions: Dict[Any, int]) -> None:
        self.role_positions = positions

    async def create_category(self, name: str) -> VCategory:
        c = VCategory(name)
        self._categories[c.id] = c
        return c

    async def _create(self, name: str, view: str, category: Any = None, private: bool = False,
                      topic: Optional[str] = None) -> VChannel:
        ch = VChannel(self, name, view, category_id=category.id if category else None, private=private, topic=topic)
        self._channels[ch.id] = ch
        return ch

    async def create_text_channel(self, name: str, **kw: Any) -> VChannel:
        return await self._create(name, "chat", **kw)

    async def create_voice_channel(self, name: str, **kw: Any) -> VChannel:
        return await self._create(name, "voice", **kw)

    async def create_forum(self, name: str, **kw: Any) -> VChannel:
        return await self._create(name, "forum", **kw)

    async def create_invite(self, **kw: Any) -> Any:
        return SimpleNamespace(url=f"https://vectorapp.io/invite/{self.id[:8]}")


class VClient:
    def __init__(self) -> None:
        self.npub = BOT_NPUB
        self.communities: Dict[str, VCommunity] = {}
        self.users: Dict[str, Any] = {}

    def add(self, community: VCommunity) -> VCommunity:
        self.communities[community.id] = community
        return community

    def get_community(self, cid: str) -> Optional[VCommunity]:
        return self.communities.get(cid)

    async def fetch_community(self, cid: str) -> Optional[VCommunity]:
        return self.communities.get(cid)

    async def create_community(self, name: str, *, description=None, relays=None, first_channel=None) -> VCommunity:
        c = self.add(VCommunity(name))
        if first_channel:
            await c.create_text_channel(first_channel)
        return c

    def get_channel(self, cid: str) -> Optional[VChannel]:
        for c in self.communities.values():
            if cid in c._channels:
                return c._channels[cid]
        return None

    def get_user(self, npub: str) -> Any:
        return self.users.get(npub)


# --------------------------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------------------------


class FakeWebhook:
    def __init__(self) -> None:
        self.id = snowflake()
        self.token = "tok"
        self.sent: List[Dict[str, Any]] = []
        self.edited: List[tuple] = []
        self.deleted: List[int] = []

    async def send(self, content: str = None, **kw: Any) -> Any:
        m = SimpleNamespace(id=snowflake(), content=content)
        self.sent.append({"content": content, **kw, "id": m.id})
        return m

    async def edit_message(self, mid: int, **kw: Any) -> None:
        self.edited.append((mid, kw.get("content")))

    async def delete_message(self, mid: int, **kw: Any) -> None:
        self.deleted.append(mid)


def guild(name: str = "Guild", *, emojis: Optional[list] = None) -> Any:
    g = MagicMock(spec=discord.Guild)
    g.id = snowflake()
    g.name = name
    g.emojis = emojis or []
    g.description = None
    g.icon = None
    g.banner = None
    return g


def text_channel(g: Any, name: str = "general", *, category: Any = None, position: int = 0,
                 overwrites: Optional[dict] = None) -> Any:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = snowflake()
    ch.name = name
    ch.guild = g
    ch.category = category
    ch.position = position
    ch.topic = None
    ch.overwrites = overwrites or {}
    ch.sent = []

    async def send(content: str = None, **kw: Any) -> Any:
        m = SimpleNamespace(id=snowflake(), content=content)
        ch.sent.append({"content": content, **kw})
        return m

    ch.send = send
    return ch


def dmessage(channel: Any, content: str, *, author: str = "Ana", bot: bool = False, webhook_id: Optional[int] = None,
             reference: Any = None, attachments: Optional[list] = None) -> Any:
    return SimpleNamespace(
        id=snowflake(), channel=channel, guild=channel.guild, content=content, clean_content=content,
        author=SimpleNamespace(id=snowflake(), display_name=author, bot=bot),
        webhook_id=webhook_id, type=discord.MessageType.default, reference=reference,
        attachments=attachments or [], embeds=[], stickers=[],
        created_at=datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc),
    )


def dclient(*channels: Any) -> Any:
    by_id = {c.id: c for c in channels}
    return SimpleNamespace(user=SimpleNamespace(id=1), get_channel=lambda i: by_id.get(i),
                           get_guild=lambda i: None)


def settings(**kw: Any) -> Settings:
    s = Settings(discord_token="x", clone_delay=0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s
