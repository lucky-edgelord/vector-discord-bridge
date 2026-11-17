"""Message relay between paired Discord and Vector channels: send, reply, edit and delete in both directions."""

from __future__ import annotations

import datetime
import io
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import discord
import vector

from . import formatting as fmt
from .store import MessageLink, Store

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger("vdbridge.relay")

WEBHOOK_NAME = "Vector Bridge"
_HEADER = re.compile(r"^\*\*(?P<name>[^*\n]+)\*\*[^\n]*\n?(?P<rest>.*)$", re.DOTALL)
_RELAYED_TYPES = {discord.MessageType.default, discord.MessageType.reply}


def split_header(text: str) -> Tuple[Optional[str], str]:
    """Undo :func:`formatting.vector_header`: ``"**Ana**\\nhi"`` → ``("Ana", "hi")``."""
    m = _HEADER.match(text or "")
    return (m.group("name"), m.group("rest")) if m else (None, text or "")


class Relay:
    def __init__(self, discord_client: discord.Client, vector_client: vector.Client, store: Store,
                 settings: "Settings"):
        self.discord = discord_client
        self.vector = vector_client
        self.store = store
        self.settings = settings
        self._webhooks: Dict[int, Optional[discord.Webhook]] = {}
        self._own_webhook_ids = set(store.webhook_ids())

    # ------------------------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------------------------

    def is_own_discord(self, message: discord.Message) -> bool:
        """Messages the bridge itself posted on Discord (bot account or one of its webhooks)."""
        if message.webhook_id is not None and message.webhook_id in self._own_webhook_ids:
            return True
        me = self.discord.user
        return me is not None and message.author.id == me.id

    def is_own_vector(self, message: Any) -> bool:
        return bool(getattr(message, "mine", False)) or getattr(message.author, "npub", None) == self.vector.npub

    def vector_channel_for(self, channel: Any) -> Tuple[Optional[Any], Optional[str]]:
        """The Vector twin of a Discord channel; threads map to their parent with the thread name as a tag."""
        thread = None
        if isinstance(channel, discord.Thread):
            thread = channel.name
            channel = channel.parent
            if channel is None:
                return None, None
        vid = self.store.vector_id("channel", channel.id)
        if not vid:
            return None, None
        return self.vector.get_channel(vid), thread

    def discord_channel_for(self, vector_channel_id: str) -> Optional[Any]:
        did = self.store.discord_id("channel", vector_channel_id)
        return self.discord.get_channel(did) if did else None

    def _name_for_npub(self, npub: str) -> Optional[str]:
        user = self.vector.get_user(npub)
        return user.display_name if user is not None else None

    def _guild_emojis(self, guild: Optional[discord.Guild]) -> Dict[str, str]:
        return {e.name: str(e) for e in guild.emojis} if guild else {}

    # ------------------------------------------------------------------------------------
    # Discord -> Vector
    # ------------------------------------------------------------------------------------

    def _vector_text(self, message: discord.Message, *, thread: Optional[str], historical: bool) -> str:
        quote = None
        ref = message.reference
        if ref is not None and ref.message_id and not self.store.by_discord(ref.message_id):
            # the replied-to message isn't on Vector, so quote it instead of a native reply
            resolved = ref.resolved
            if isinstance(resolved, discord.Message):
                quote = fmt.reply_quote(resolved.author.display_name, resolved.clean_content)
        return fmt.to_vector(
            message.clean_content, message.author.display_name, thread=thread, quote=quote,
            extras=[f"[sticker: {s.name}]" for s in message.stickers],
            stamp=int(message.created_at.timestamp()) if historical else None,
        )

    async def discord_to_vector(self, message: discord.Message, *, historical: bool = False) -> List[str]:
        """Relay one Discord message. Returns the Vector message ids created (text first, then files)."""
        if message.type not in _RELAYED_TYPES or self.is_own_discord(message):
            return []
        if message.author.bot and message.webhook_id is None and not self.settings.relay_bots:
            return []
        if self.store.by_discord(message.id):
            return []  # already relayed
        vch, thread = self.vector_channel_for(message.channel)
        if vch is None:
            return []

        text = self._vector_text(message, thread=thread, historical=historical)
        reference = None
        if message.reference and message.reference.message_id:
            links = self.store.by_discord(message.reference.message_id)
            reference = links[0].vector_id if links else None

        embeds = [vector.Embed.from_dict(e.to_dict()) for e in message.embeds if e.type == "rich"]
        files: List[vector.File] = []
        limit = self.settings.max_attachment_mb * 1024 * 1024
        oversized: List[str] = []
        for a in message.attachments:
            if a.size > limit:
                oversized.append(f"📎 [{a.filename}]({a.url})")
                continue
            try:
                files.append(vector.File(await a.read(), filename=a.filename, spoiler=a.is_spoiler()))
            except discord.HTTPException:
                oversized.append(f"📎 [{a.filename}]({a.url})")
        if oversized:
            text = f"{text}\n" + "\n".join(oversized)

        ids: List[str] = []
        sent = await vch.send(text, embeds=embeds or None, reference=reference)
        ids.append(sent.id)
        for f in files:
            try:
                ids.append((await vch.send(file=f)).id)
            except Exception:
                log.exception("could not upload %s to Vector", f.filename)
        for vid in ids:
            self.store.add_message(message.id, vid, message.channel.id, vch.id, "discord")
        return ids

    async def discord_edited(self, message: discord.Message) -> None:
        if self.is_own_discord(message):
            return
        links = [l for l in self.store.by_discord(message.id) if l.origin == "discord"]
        if not links:
            return
        vch = self.vector.get_channel(links[0].vector_channel)
        if vch is None:
            return
        _, thread = self.vector_channel_for(message.channel)
        text = self._vector_text(message, thread=thread, historical=False)
        try:
            current = await vch.fetch_message(links[0].vector_id)
            if current.content == text:
                return  # e.g. Discord added a link preview; nothing visible changed
        except Exception:
            pass
        await vch.get_partial_message(links[0].vector_id).edit(content=text)

    async def discord_deleted(self, message_id: int) -> None:
        links = self.store.by_discord(message_id)
        if not links:
            return
        self.store.forget_discord(message_id)  # first, so the echo from Vector finds nothing
        for link in links:
            vch = self.vector.get_channel(link.vector_channel)
            if vch is None:
                continue
            try:
                # fetched messages know whether they're ours (delete) or a member's (hide, i.e. moderation)
                msg = await vch.fetch_message(link.vector_id)
                await msg.delete()
            except Exception:
                try:
                    await vch.get_partial_message(link.vector_id).delete()
                except Exception:
                    log.warning("could not delete Vector message %s", link.vector_id[:12])

    # ------------------------------------------------------------------------------------
    # Vector -> Discord
    # ------------------------------------------------------------------------------------

    async def webhook_for(self, channel: Any) -> Optional[discord.Webhook]:
        """The bridge's webhook in this channel (created on first use), or None if not allowed."""
        if not self.settings.use_webhooks:
            return None
        if channel.id in self._webhooks:
            return self._webhooks[channel.id]
        hook: Optional[discord.Webhook] = None
        saved = self.store.webhook(channel.id)
        if saved:
            hook = discord.Webhook.partial(saved[0], saved[1], client=self.discord)
        else:
            try:
                hook = await channel.create_webhook(name=WEBHOOK_NAME)
                self.store.set_webhook(channel.id, hook.id, hook.token)
            except (discord.Forbidden, AttributeError):
                log.warning("no webhook permission in #%s; Vector users will appear as the bot", channel)
                hook = None
            except discord.HTTPException as e:
                log.warning("could not create webhook in #%s: %s", channel, e)
                return None  # don't cache: may be transient
        if hook is not None:
            self._own_webhook_ids.add(hook.id)
        self._webhooks[channel.id] = hook
        return hook

    def _forget_webhook(self, channel_id: int) -> None:
        self._webhooks.pop(channel_id, None)
        self.store.drop_webhook(channel_id)

    async def forum_thread(self, forum: discord.ForumChannel) -> Optional[discord.Thread]:
        """Forums need a post to write in: Vector messages go to one persistent "Vector chat" post."""
        tid = self.store.vector_id("forum_thread", forum.id)
        if tid:
            thread = forum.get_thread(int(tid))
            if thread is None:
                try:
                    thread = await self.discord.fetch_channel(int(tid))  # type: ignore[assignment]
                except discord.HTTPException:
                    thread = None
            if thread is not None:
                return thread  # type: ignore[return-value]
        try:
            created = await forum.create_thread(name="Vector chat", content="Messages from the Vector side of this forum.")
        except discord.HTTPException:
            return None
        self.store.link("forum_thread", forum.id, str(created.thread.id), forum.guild.id)
        return created.thread

    def _discord_text(self, message: Any, guild: Optional[discord.Guild], *, with_name: bool) -> str:
        body = fmt.to_discord(message.content, resolve_npub=self._name_for_npub, emojis=self._guild_emojis(guild))
        ref = getattr(message, "reference", None)
        if ref is not None and ref.message_id:
            links = self.store.by_vector(ref.message_id)
            ref_name, ref_text = split_header(ref.content or "")
            if ref_name is None and ref.author_id:
                ref_name = self._name_for_npub(ref.author_id)
            quote = fmt.reply_quote(ref_name, fmt.to_discord(ref_text, resolve_npub=self._name_for_npub))
            if links and guild is not None:
                l = links[0]
                quote += f" [↗](https://discord.com/channels/{guild.id}/{l.discord_channel}/{l.discord_id})"
            body = f"{quote}\n{body}" if quote else body
        if with_name:
            body = f"**{message.author.display_name}**: {body}"
        return body

    async def vector_to_discord(self, message: Any) -> List[int]:
        if self.is_own_vector(message) or self.store.by_vector(message.id):
            return []
        created = getattr(message, "created_at", None)
        if created is not None and self.settings.catch_up_hours >= 0:
            # on restart Vector replays what was missed; don't dump very old history into Discord
            age = datetime.datetime.now(datetime.timezone.utc) - created
            if age > datetime.timedelta(hours=self.settings.catch_up_hours):
                return []
        dch = self.discord_channel_for(message.channel.id)
        if dch is None:
            return []
        target = dch
        thread_kw: Dict[str, Any] = {}
        if isinstance(dch, discord.ForumChannel):
            thread = await self.forum_thread(dch)
            if thread is None:
                return []
            thread_kw = {"thread": thread}
            target = thread

        files: List[discord.File] = []
        oversized: List[str] = []
        limit = self.settings.max_attachment_mb * 1024 * 1024
        for a in message.attachments:
            if a.size and a.size > limit:
                oversized.append(f"📎 {a.filename} (too large to bridge)")
                continue
            try:
                files.append(discord.File(io.BytesIO(await a.read()), filename=a.filename, spoiler=a.is_spoiler()))
            except Exception:
                oversized.append(f"📎 {a.filename} (could not download)")

        hook = await self.webhook_for(dch)
        text = self._discord_text(message, dch.guild, with_name=hook is None)
        if oversized:
            text = "\n".join([text, *oversized]) if text else "\n".join(oversized)
        chunks = fmt.split_discord(text) if text else [""]
        none = discord.AllowedMentions.none()
        ids: List[int] = []

        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            kw: Dict[str, Any] = {"allowed_mentions": none}
            if last and files:
                kw["files"] = files
            if not chunk and not kw.get("files"):
                continue
            if hook is not None:
                avatar = getattr(message.author, "avatar", None)
                avatar_url = getattr(avatar, "url", None)
                try:
                    sent = await hook.send(
                        chunk, wait=True,
                        username=fmt.webhook_username(message.author.display_name, self.settings.name_suffix),
                        avatar_url=avatar_url if avatar_url and avatar_url.startswith("http") else None,
                        **thread_kw, **kw)
                except discord.NotFound:
                    self._forget_webhook(dch.id)  # deleted on Discord; recreate next time
                    hook = None
                    sent = await target.send(f"**{message.author.display_name}**: {chunk}", **kw)
            else:
                sent = await target.send(chunk, **kw)
            ids.append(sent.id)
        for did in ids:
            self.store.add_message(did, message.id, target.id, message.channel.id, "vector")
        return ids

    async def _discord_message_ops(self, link: MessageLink) -> Tuple[Optional[discord.Webhook], Any, Dict[str, Any]]:
        """How to edit/delete a message we posted: through the webhook (with thread) or as the bot."""
        channel = self.discord.get_channel(int(link.discord_channel))
        if channel is None:
            return None, None, {}
        parent = channel.parent if isinstance(channel, discord.Thread) else channel
        thread_kw = {"thread": channel} if isinstance(channel, discord.Thread) else {}
        saved = self.store.webhook(parent.id)
        hook = discord.Webhook.partial(saved[0], saved[1], client=self.discord) if saved else None
        return hook, channel, thread_kw

    async def vector_edited(self, message: Any) -> None:
        if self.is_own_vector(message):
            return
        links = [l for l in self.store.by_vector(message.id) if l.origin == "vector"]
        if not links:
            return
        hook, channel, thread_kw = await self._discord_message_ops(links[0])
        if channel is None:
            return
        text = self._discord_text(message, channel.guild, with_name=hook is None)
        text = fmt.split_discord(text)[0] if text else "​"
        try:
            if hook is not None:
                await hook.edit_message(int(links[0].discord_id), content=text,
                                        allowed_mentions=discord.AllowedMentions.none(), **thread_kw)
            else:
                await channel.get_partial_message(int(links[0].discord_id)).edit(content=text)
        except discord.HTTPException as e:
            log.warning("could not edit Discord message %s: %s", links[0].discord_id, e)

    async def vector_deleted(self, message_id: str, deleted_by: Optional[str]) -> None:
        if deleted_by and deleted_by == self.vector.npub:
            return  # our own delete, mirroring a Discord delete
        links = self.store.by_vector(message_id)
        if not links:
            return
        self.store.forget_vector(message_id)
        for link in links:
            hook, channel, thread_kw = await self._discord_message_ops(link)
            if channel is None:
                continue
            try:
                if link.origin == "vector" and hook is not None:
                    await hook.delete_message(int(link.discord_id), **thread_kw)
                else:
                    # Vector-side moderation of a Discord user's message, or our own bot message
                    await channel.get_partial_message(int(link.discord_id)).delete()
            except discord.Forbidden:
                log.info("no permission to delete Discord message %s", link.discord_id)
            except discord.HTTPException:
                pass
