"""Run the Discord bot and the Vector bot in one process and wire their events to the relay and cloner."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import discord
from discord import app_commands
import vector
from vector.ext import commands as vcommands

from .cloner import Cloner, channel_view
from .config import Settings
from .relay import Relay
from .store import Store

log = logging.getLogger("vdbridge")


def make_discord_client() -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True  # privileged: enable "Message Content Intent" in the developer portal
    return discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())


def make_vector_bot(settings: Settings) -> vcommands.Bot:
    return vcommands.Bot(command_prefix="!", data_dir=settings.vector_data_dir, help_command=None)


class Bridge:
    """Pairs every linked Discord guild with its Vector Community."""

    def __init__(self, settings: Settings, *, discord_client: Optional[discord.Client] = None,
                 vector_bot: Optional[Any] = None, store: Optional[Store] = None):
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.discord = discord_client or make_discord_client()
        self.vector = vector_bot or make_vector_bot(settings)
        self.relay = Relay(self.discord, self.vector, self.store, settings)
        self.cloner = Cloner(self.discord, self.vector, self.store, settings,
                             history=lambda m: self.relay.discord_to_vector(m, historical=True))
        self.tree = app_commands.CommandTree(self.discord)
        self._installed = False

    # ------------------------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------------------------

    def linked(self, guild: Optional[discord.Guild]) -> bool:
        return guild is not None and self.store.vector_id("guild", guild.id) is not None

    def linked_community(self, community: Optional[Any]) -> bool:
        return community is not None and self.store.discord_id("guild", community.id) is not None

    async def vector_invite(self, guild: discord.Guild) -> Optional[str]:
        url = self.store.vector_id("invite", guild.id)
        if url:
            return url
        community = self.cloner.community_for(guild)
        if community is None:
            return None
        invite = await community.create_invite(label=f"Discord: {guild.name}")
        self.store.link("invite", guild.id, invite.url, guild.id)
        return invite.url

    async def discord_invite(self, community_id: str) -> Optional[str]:
        if self.settings.discord_invite_url:
            return self.settings.discord_invite_url
        gid = self.store.discord_id("guild", community_id)
        guild = self.discord.get_guild(gid) if gid else None
        if guild is None:
            return None
        channel = guild.system_channel or next(iter(guild.text_channels), None)
        if channel is None:
            return None
        try:
            return (await channel.create_invite(max_age=0, unique=False, reason="Vector bridge")).url
        except discord.HTTPException:
            return None

    # ------------------------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------------------------

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        d, v, relay, cloner = self.discord, self.vector, self.relay, self.cloner
        sync = self.settings.sync_structure

        async def guarded(coro: Any, what: str) -> None:
            try:
                await coro
            except Exception:
                log.exception("%s failed", what)

        # -- Discord: messages -------------------------------------------------------------

        @d.event
        async def on_message(message: discord.Message) -> None:
            if self.linked(message.guild):
                await guarded(relay.discord_to_vector(message), "Discord → Vector")

        @d.event
        async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent) -> None:
            if payload.guild_id is None or "content" not in payload.data:
                return
            message = getattr(payload, "message", None)
            if message is None:
                channel = d.get_channel(payload.channel_id)
                if channel is None:
                    return
                try:
                    message = await channel.fetch_message(payload.message_id)  # type: ignore[union-attr]
                except discord.HTTPException:
                    return
            await guarded(relay.discord_edited(message), "Discord edit")

        @d.event
        async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
            await guarded(relay.discord_deleted(payload.message_id), "Discord delete")

        @d.event
        async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent) -> None:
            for mid in payload.message_ids:
                await guarded(relay.discord_deleted(mid), "Discord bulk delete")

        @d.event
        async def on_webhooks_update(channel: Any) -> None:
            relay._webhooks.pop(channel.id, None)  # re-check our webhook on next use

        # -- Discord: structure -------------------------------------------------------------

        @d.event
        async def on_guild_channel_create(channel: Any) -> None:
            community = cloner.community_for(channel.guild)
            if not sync or community is None:
                return
            if isinstance(channel, discord.CategoryChannel):
                await guarded(cloner.sync_category(channel, community), "category create")
            elif channel_view(channel):
                await guarded(cloner.sync_channel(channel, community), "channel create")

        @d.event
        async def on_guild_channel_update(before: Any, after: Any) -> None:
            community = cloner.community_for(after.guild)
            if not sync or community is None:
                return
            if isinstance(after, discord.CategoryChannel):
                await guarded(cloner.sync_category(after, community), "category update")
                dest_id = self.store.vector_id("category", after.id)
                dest = community.get_category(dest_id) if dest_id else None
            elif channel_view(after):
                await guarded(cloner.sync_channel(after, community), "channel update")
                dest_id = self.store.vector_id("channel", after.id)
                dest = community.get_channel(dest_id) if dest_id else None
            else:
                return
            # overwrites removed on Discord are cleared on Vector
            if dest is not None:
                for target in set(before.overwrites) - set(after.overwrites):
                    vt = cloner.vector_target(target, community)
                    if vt is not None:
                        await guarded(dest.set_permissions(vt, overwrite=vector.PermissionOverwrite()),
                                      "clear overwrite")

        @d.event
        async def on_guild_channel_delete(channel: Any) -> None:
            if not sync or not self.linked(channel.guild):
                return
            if isinstance(channel, discord.CategoryChannel):
                await guarded(cloner.remove_category(channel), "category delete")
            else:
                await guarded(cloner.archive_channel(channel), "channel archive")
                relay._webhooks.pop(channel.id, None)
                self.store.drop_webhook(channel.id)

        @d.event
        async def on_guild_role_create(role: discord.Role) -> None:
            community = cloner.community_for(role.guild)
            if sync and community is not None:
                await guarded(cloner.sync_role(role, community), "role create")

        @d.event
        async def on_guild_role_update(before: discord.Role, after: discord.Role) -> None:
            community = cloner.community_for(after.guild)
            if not sync or community is None:
                return
            if after.is_default():
                from .perms import to_vector_bits

                await guarded(community.set_default_permissions(to_vector_bits(after.permissions)), "@everyone")
            else:
                await guarded(cloner.sync_role(after, community), "role update")

        @d.event
        async def on_guild_role_delete(role: discord.Role) -> None:
            if sync and self.linked(role.guild):
                await guarded(cloner.remove_role(role), "role delete")

        @d.event
        async def on_guild_update(before: discord.Guild, after: discord.Guild) -> None:
            community = cloner.community_for(after)
            if sync and community is not None and (
                    before.name != after.name or before.icon != after.icon or before.banner != after.banner
                    or before.description != after.description):
                await guarded(cloner.sync_meta(after, community), "server update")

        # -- Discord: commands --------------------------------------------------------------

        @self.tree.command(name="vector", description="Get the link to this server's Vector Community")
        async def vector_cmd(interaction: discord.Interaction) -> None:
            url = await self.vector_invite(interaction.guild) if interaction.guild else None
            await interaction.response.send_message(
                f"Join this community on Vector: {url}" if url else "This server isn't bridged to Vector yet.",
                ephemeral=True)

        @d.event
        async def on_ready() -> None:
            log.info("Discord: logged in as %s", d.user)
            for gid, _ in self.store.links("guild"):
                guild = d.get_guild(gid)
                if guild is None:
                    log.warning("Discord: the bot is not in linked server %s", gid)
                    continue
                self.tree.copy_global_to(guild=guild)
                try:
                    await self.tree.sync(guild=guild)
                except discord.HTTPException:
                    log.warning("could not register /vector in %s", guild.name)

        # -- Vector --------------------------------------------------------------------------

        @v.listen("on_message")
        async def v_message(message: Any) -> None:
            if not self.linked_community(getattr(message, "community", None)):
                return
            if (message.content or "").strip().lower() in ("!discord", "/discord"):
                return
            await guarded(relay.vector_to_discord(message), "Vector → Discord")

        @v.listen("on_raw_message_edit")
        async def v_edit(payload: Any) -> None:
            if self.linked_community(getattr(payload.message, "community", None)):
                await guarded(relay.vector_edited(payload.message), "Vector edit")

        @v.listen("on_raw_message_delete")
        async def v_delete(payload: Any) -> None:
            await guarded(relay.vector_deleted(payload.message_id, payload.deleted_by), "Vector delete")

        @v.listen("on_ready")
        async def v_ready() -> None:
            log.info("Vector: logged in as %s", v.npub)

        @v.command(name="discord")
        async def discord_cmd(ctx: Any) -> None:
            """Get the invite link to the Discord side of this community."""
            url = await self.discord_invite(ctx.guild.id) if ctx.guild else None
            await ctx.reply(f"Join us on Discord: {url}" if url else "This community isn't bridged to Discord.")

    # ------------------------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------------------------

    async def start(self, *, bridge: bool = True) -> None:
        """Log both bots in and wait until both are ready.

        With ``bridge=False`` (clone-only use) no event handlers are installed: nothing is relayed or synced, and no
        commands are registered. The bots only log in so the cloner can read Discord and write to Vector.
        """
        if bridge:
            self.install()
        await self.vector.login(self.settings.vector_nsec)
        self._vector_task = asyncio.ensure_future(self.vector.connect())
        await self.discord.login(self.settings.discord_token)
        self._discord_task = asyncio.ensure_future(self.discord.connect())
        await asyncio.gather(self.discord.wait_until_ready(), self.vector.wait_until_ready())
        await self._load_communities()
        if self.settings.emoji_pack:
            try:
                await self.vector.subscribe_emoji_pack(self.settings.emoji_pack)
            except Exception:
                log.warning("Vector: could not equip emoji pack %s", self.settings.emoji_pack)
        if self.settings.prune_days:
            self.store.prune_messages(self.settings.prune_days)

    async def _load_communities(self) -> None:
        for _, cid in self.store.links("guild"):
            if self.vector.get_community(cid) is None:
                try:
                    await self.vector.fetch_community(cid)
                except Exception:
                    log.warning("Vector: could not load linked Community %s", cid[:12])

    async def run_forever(self) -> None:
        await self.start()
        done, _ = await asyncio.wait({self._vector_task, self._discord_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception():
                log.error("a connection stopped: %r", task.exception())

    async def close(self) -> None:
        for closer in (self.discord.close, self.vector.close):
            try:
                await closer()
            except Exception:
                pass
        self.store.close()
