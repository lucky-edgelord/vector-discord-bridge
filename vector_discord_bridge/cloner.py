"""Clone a Discord server's structure into a Vector Community, and keep it in sync afterwards.

Discord is the source of truth for structure: roles, categories, channels and permission overwrites are created
on Vector to match, and every pair is recorded in the :class:`Store` so the bridge knows which channels belong
together. Running the clone again is safe: anything already linked is updated instead of duplicated.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

import discord
import vector

from .perms import hides_from_everyone, to_vector_bits, to_vector_overwrite
from .store import Store

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger("vdbridge.cloner")

TEXT_TYPES = (discord.TextChannel,)  # news channels are TextChannel too
VOICE_TYPES = (discord.VoiceChannel, discord.StageChannel)
FORUM_TYPES = tuple(t for t in (getattr(discord, "ForumChannel", None), getattr(discord, "MediaChannel", None)) if t)
ARCHIVE_PREFIX = "archived-"


def channel_view(channel: Any) -> Optional[str]:
    if isinstance(channel, FORUM_TYPES):
        return "forum"
    if isinstance(channel, VOICE_TYPES):
        return "voice"
    if isinstance(channel, TEXT_TYPES):
        return "chat"
    return None


def _sort_key(channel: Any) -> tuple:
    cat = channel.category
    return (cat.position if cat else -1, channel.position, channel.id)


class Cloner:
    def __init__(self, discord_client: discord.Client, vector_client: vector.Client, store: Store,
                 settings: "Settings", *, history: Optional[Callable[[discord.Message], Awaitable[Any]]] = None):
        self.discord = discord_client
        self.vector = vector_client
        self.store = store
        self.settings = settings
        self._history = history

    async def _pause(self) -> None:
        if self.settings.clone_delay:
            await asyncio.sleep(self.settings.clone_delay)

    def community_for(self, guild: discord.Guild) -> Optional[Any]:
        cid = self.store.vector_id("guild", guild.id)
        return self.vector.get_community(cid) if cid else None

    # ------------------------------------------------------------------------------------
    # full clone
    # ------------------------------------------------------------------------------------

    async def clone(self, guild: discord.Guild, community_id: Optional[str] = None) -> Any:
        """Clone ``guild`` into ``community_id`` (or the linked / a new Community). Returns the Community."""
        community = None
        cid = community_id or self.store.vector_id("guild", guild.id)
        if cid:
            community = self.vector.get_community(cid) or await self.vector.fetch_community(cid)
        if community is None:
            first = next((c for c in sorted(guild.text_channels, key=_sort_key)), None)
            community = await self.vector.create_community(
                guild.name, description=guild.description, relays=self.settings.relays or None,
                first_channel=first.name if first else None)
            log.info("created Vector Community %s (%s)", community.name, community.id[:12])
            if first is not None:
                await community.fetch_channels()
                if community.channels:
                    self.store.link("channel", first.id, community.channels[0].id, guild.id)
        self.store.link("guild", guild.id, community.id, guild.id)

        await self.sync_meta(guild, community)
        await self.sync_roles(guild, community)
        for cat in sorted(guild.categories, key=lambda c: c.position):
            await self.sync_category(cat, community)
        for ch in sorted((c for c in guild.channels if channel_view(c)), key=_sort_key):
            await self.sync_channel(ch, community)
        if self.settings.clone_history and self._history is not None:
            await self.copy_history(guild)
        log.info("clone of %s complete", guild.name)
        return community

    async def sync_meta(self, guild: discord.Guild, community: Any) -> None:
        kw: Dict[str, Any] = {}
        if community.name != guild.name:
            kw["name"] = guild.name
        if (guild.description or None) != (community.description or None):
            kw["description"] = guild.description or ""
        for attr in ("icon", "banner"):
            asset = getattr(guild, attr)
            if asset is not None:
                try:
                    kw[attr] = await asset.read()
                except discord.HTTPException:
                    log.warning("could not download the server %s", attr)
        if kw:
            await community.edit(**kw)
            await self._pause()

    # -- roles ----------------------------------------------------------------------------

    async def sync_roles(self, guild: discord.Guild, community: Any) -> None:
        await community.set_default_permissions(to_vector_bits(guild.default_role.permissions))
        positions = {}
        for role in sorted(guild.roles, key=lambda r: r.position):
            if role.is_default():
                continue
            vrole = await self.sync_role(role, community)
            if vrole is not None:
                positions[vrole] = role.position
        if positions:
            try:
                await community.edit_role_positions(positions)
            except Exception:
                log.warning("could not reorder Vector roles", exc_info=True)

    async def sync_role(self, role: discord.Role, community: Any) -> Optional[Any]:
        if role.managed:  # bot / booster / integration roles are owned by Discord
            return None
        kw = dict(name=role.name, permissions=to_vector_bits(role.permissions), colour=role.colour.value,
                  hoist=role.hoist, mentionable=role.mentionable)
        vid = self.store.vector_id("role", role.id)
        existing = community.get_role(vid) if vid else None
        if existing is not None:
            if (existing.name, int(existing.permissions), existing.colour.value, existing.hoist, existing.mentionable) != \
                    (kw["name"], kw["permissions"], kw["colour"], kw["hoist"], kw["mentionable"]):
                await existing.edit(**kw)
                await self._pause()
            return existing
        vrole = await community.create_role(**kw)
        self.store.link("role", role.id, vrole.id, role.guild.id)
        log.info("role %s", role.name)
        await self._pause()
        return vrole

    def vector_target(self, target: Any, community: Any) -> Optional[Any]:
        """The Vector object an overwrite target maps to (Discord members have no Vector twin)."""
        if isinstance(target, discord.Role):
            if target.is_default():
                return community.default_role
            vid = self.store.vector_id("role", target.id)
            return community.get_role(vid) if vid else None
        return None

    async def apply_overwrites(self, source: Any, dest: Any, community: Any) -> None:
        for target, ow in source.overwrites.items():
            vtarget = self.vector_target(target, community)
            if vtarget is None:
                continue
            values = to_vector_overwrite(ow)
            if not values:
                continue
            try:
                await dest.set_permissions(vtarget, overwrite=vector.PermissionOverwrite(**values))
            except Exception:
                log.warning("could not copy an overwrite on %s", source.name, exc_info=True)

    # -- categories & channels -------------------------------------------------------------

    async def sync_category(self, cat: discord.CategoryChannel, community: Any) -> Any:
        vid = self.store.vector_id("category", cat.id)
        vcat = community.get_category(vid) if vid else None
        if vcat is None:
            vcat = await community.create_category(cat.name)
            self.store.link("category", cat.id, vcat.id, cat.guild.id)
            log.info("category %s", cat.name)
        elif vcat.name != cat.name:
            await vcat.edit(name=cat.name)
        await self.apply_overwrites(cat, vcat, community)
        await self._pause()
        return vcat

    async def sync_channel(self, ch: Any, community: Any) -> Optional[Any]:
        view = channel_view(ch)
        if view is None:
            return None
        hidden = hides_from_everyone(ch.overwrites, ch.guild.default_role)
        if hidden and self.settings.hidden_channels == "skip":
            return None
        private = hidden and self.settings.hidden_channels == "private"

        vcat = None
        if ch.category is not None:
            cat_vid = self.store.vector_id("category", ch.category.id)
            vcat = community.get_category(cat_vid) if cat_vid else None
            if vcat is None:
                vcat = await self.sync_category(ch.category, community)

        topic = getattr(ch, "topic", None)
        vid = self.store.vector_id("channel", ch.id)
        vch = community.get_channel(vid) if vid else None
        if vch is None:
            create = {"chat": community.create_text_channel, "voice": community.create_voice_channel,
                      "forum": community.create_forum}[view]
            kw: Dict[str, Any] = {"category": vcat, "private": private}
            if view != "voice":
                kw["topic"] = topic
            vch = await create(ch.name, **kw)
            self.store.link("channel", ch.id, vch.id, ch.guild.id)
            log.info("channel #%s (%s)", ch.name, view)
        else:
            changes: Dict[str, Any] = {}
            if vch.name != ch.name:
                changes["name"] = ch.name
            if view != "voice" and (vch.topic or None) != (topic or None):
                changes["topic"] = topic or ""
            if (vch.category_id or None) != (vcat.id if vcat else None):
                changes["category"] = vcat
            if vch.view != view:
                changes["view"] = view
            if self.settings.hidden_channels == "private" and bool(vch.private) != private:
                changes["private"] = private
            if changes:
                vch = await vch.edit(**changes)
        await self.apply_overwrites(ch, vch, community)
        await self._pause()
        return vch

    async def archive_channel(self, ch: Any) -> None:
        """A Discord channel was deleted: keep the Vector channel (and its history) but archive and unlink it."""
        vid = self.store.vector_id("channel", ch.id)
        community = self.community_for(ch.guild)
        if not vid or community is None:
            return
        self.store.unlink("channel", ch.id)
        vch = community.get_channel(vid)
        if vch is None:
            return
        try:
            await vch.edit(name=f"{ARCHIVE_PREFIX}{vch.name}"[:100], read_only=True)
        except Exception:
            log.warning("could not archive Vector channel %s", vch.name, exc_info=True)

    async def remove_category(self, cat: discord.CategoryChannel) -> None:
        vid = self.store.vector_id("category", cat.id)
        community = self.community_for(cat.guild)
        self.store.unlink("category", cat.id)
        vcat = community.get_category(vid) if community and vid else None
        if vcat is not None:
            try:
                await vcat.delete()
            except Exception:
                log.warning("could not delete Vector category %s", vcat.name, exc_info=True)

    async def remove_role(self, role: discord.Role) -> None:
        vid = self.store.vector_id("role", role.id)
        community = self.community_for(role.guild)
        self.store.unlink("role", role.id)
        vrole = community.get_role(vid) if community and vid else None
        if vrole is not None:
            await vrole.delete()

    # -- history & emoji ---------------------------------------------------------------------

    async def copy_history(self, guild: discord.Guild) -> None:
        limit = self.settings.clone_history
        me = guild.me
        for ch in sorted(guild.text_channels, key=_sort_key):
            if not self.store.vector_id("channel", ch.id) or not ch.permissions_for(me).read_message_history:
                continue
            msgs: List[discord.Message] = [m async for m in ch.history(limit=limit)]
            for m in reversed(msgs):
                try:
                    await self._history(m)  # type: ignore[misc]
                except Exception:
                    log.warning("history: skipped a message in #%s", ch.name, exc_info=True)
            log.info("history: #%s (%d messages)", ch.name, len(msgs))

    @staticmethod
    async def download_emojis(guild: discord.Guild, directory: str) -> int:
        """Save the server's custom emoji as files, ready to import into a Vector emoji pack."""
        out = os.path.join(directory, str(guild.id))
        os.makedirs(out, exist_ok=True)
        n = 0
        for e in guild.emojis:
            path = os.path.join(out, f"{e.name}.{'gif' if e.animated else 'png'}")
            try:
                with open(path, "wb") as fh:
                    fh.write(await e.read())
                n += 1
            except discord.HTTPException:
                log.warning("could not download emoji %s", e.name)
        return n
