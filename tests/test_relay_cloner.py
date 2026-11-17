from types import SimpleNamespace

import discord
import pytest

from vector_discord_bridge.cloner import Cloner
from vector_discord_bridge.relay import Relay
from vector_discord_bridge.store import Store

from . import fakes as f


@pytest.fixture
def world(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "b.db"))
    vc = f.VClient()
    community = vc.add(f.VCommunity())
    g = f.guild()
    dch = f.text_channel(g)
    vch = f.VChannel(community, "general")
    community._channels[vch.id] = vch
    store.link("guild", g.id, community.id, g.id)
    store.link("channel", dch.id, vch.id, g.id)
    hook = f.FakeWebhook()
    store.set_webhook(dch.id, hook.id, hook.token)
    monkeypatch.setattr(discord.Webhook, "partial", classmethod(lambda cls, *a, **k: hook))
    relay = Relay(f.dclient(dch), vc, store, f.settings())
    return SimpleNamespace(store=store, vc=vc, community=community, g=g, dch=dch, vch=vch, hook=hook, relay=relay)


def vector_msg(w, content, **kw):
    author = SimpleNamespace(npub=f.ALICE_NPUB, display_name="Alice", avatar=SimpleNamespace(url="https://x/a.png"))
    return f.VMessage(w.vch, content, author=author, **kw)


async def test_discord_to_vector_text_reply_and_files(world):
    w = world
    first = f.dmessage(w.dch, "hello")
    [v1] = await w.relay.discord_to_vector(first)
    assert w.vch.sent[0]["content"] == "**Ana**\nhello"

    att = SimpleNamespace(size=3, filename="a.png", url="u", is_spoiler=lambda: False, read=_async(b"png"))
    reply = f.dmessage(w.dch, "re", author="Bo", reference=SimpleNamespace(message_id=first.id, resolved=first),
                       attachments=[att])
    ids = await w.relay.discord_to_vector(reply)
    assert len(ids) == 2
    assert w.vch.sent[1]["reference"] == v1  # native Vector reply
    assert w.vch.sent[2]["file"].filename == "a.png"
    assert [l.vector_id for l in w.store.by_discord(reply.id)] == ids
    # relaying the same message again is a no-op
    assert await w.relay.discord_to_vector(reply) == []


async def test_discord_skips_own_webhook_and_bot(world):
    w = world
    assert await w.relay.discord_to_vector(f.dmessage(w.dch, "echo", webhook_id=w.hook.id)) == []
    me = f.dmessage(w.dch, "me")
    me.author.id = 1  # the bridge bot
    assert await w.relay.discord_to_vector(me) == []
    w.relay.settings.relay_bots = False
    assert await w.relay.discord_to_vector(f.dmessage(w.dch, "beep", bot=True)) == []
    assert w.vch.sent == []


async def test_discord_edit_and_delete_follow(world):
    w = world
    m = f.dmessage(w.dch, "v1")
    [vid] = await w.relay.discord_to_vector(m)
    m.clean_content = "v2"
    await w.relay.discord_edited(m)
    assert w.vch.edits == [(vid, "**Ana**\nv2")]
    await w.relay.discord_edited(m)  # unchanged -> no second edit
    assert len(w.vch.edits) == 1
    await w.relay.discord_deleted(m.id)
    assert w.vch.deleted == [vid] and w.store.by_discord(m.id) == []


async def test_vector_to_discord_via_webhook_with_reply_link(world):
    w = world
    d = f.dmessage(w.dch, "question")
    [v_of_d] = await w.relay.discord_to_vector(d)
    ref = SimpleNamespace(message_id=v_of_d, content="**Ana**\nquestion", author_id=None)
    msg = vector_msg(w, "answer :wave:", reference=ref)
    w.g.emojis = [_Emoji("wave", 5)]
    [did] = await w.relay.vector_to_discord(msg)
    sent = w.hook.sent[0]
    assert sent["username"] == "Alice (Vector)" and sent["avatar_url"] == "https://x/a.png"
    assert sent["content"].startswith("> ↪ **Ana**: question [↗](https://discord.com/channels/")
    assert sent["content"].endswith(f"/{d.id})\nanswer <:wave:5>")
    assert sent["allowed_mentions"].everyone is False
    assert w.store.by_vector(msg.id)[0].discord_id == str(did)
    # own Vector messages and repeats are ignored
    assert await w.relay.vector_to_discord(msg) == []
    assert await w.relay.vector_to_discord(f.VMessage(w.vch, "mine", mine=True)) == []


async def test_vector_edit_and_delete_follow(world):
    w = world
    msg = vector_msg(w, "one")
    [did] = await w.relay.vector_to_discord(msg)
    msg.content = "two"
    await w.relay.vector_edited(msg)
    assert w.hook.edited == [(did, "two")]
    await w.relay.vector_deleted(msg.id, deleted_by=f.BOT_NPUB)  # our own mirror delete: ignored
    assert w.hook.deleted == []
    await w.relay.vector_deleted(msg.id, deleted_by=f.ALICE_NPUB)
    assert w.hook.deleted == [did] and w.store.by_vector(msg.id) == []


async def test_vector_old_catch_up_is_not_dumped(world):
    import datetime

    old = vector_msg(world, "ancient")
    old.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
    assert await world.relay.vector_to_discord(old) == []


async def test_vector_without_webhook_falls_back_to_bot(world):
    w = world
    w.relay.settings.use_webhooks = False
    await w.relay.vector_to_discord(vector_msg(w, "hi"))
    assert w.dch.sent[0]["content"] == "**Alice**: hi"


async def test_clone_structure(tmp_path):
    store = Store(str(tmp_path / "c.db"))
    vc = f.VClient()
    g = f.guild("Cool Server")
    everyone = _role(g, "@everyone", 0, default=True, perms=discord.Permissions(view_channel=True, send_messages=True))
    mods = _role(g, "Mods", 2, perms=discord.Permissions(kick_members=True))
    bot_role = _role(g, "SomeBot", 3, managed=True)
    g.roles = [everyone, mods, bot_role]
    g.default_role = everyone
    cat = _category(g, "Info", 0)
    general = f.text_channel(g, "general", category=cat, position=0)
    staff = f.text_channel(g, "staff", category=cat, position=1, overwrites={
        everyone: discord.PermissionOverwrite(view_channel=False), mods: discord.PermissionOverwrite(view_channel=True)})
    voice = _voice(g, "Lounge", 2)
    g.categories = [cat]
    g.text_channels = [general, staff]
    g.channels = [cat, general, staff, voice]

    cloner = Cloner(f.dclient(), vc, store, f.settings())
    community = await cloner.clone(g)
    assert community.name == "Cool Server"
    assert store.vector_id("guild", g.id) == community.id
    assert {r.name for r in community._roles.values()} == {"Mods"}  # managed role skipped
    names = {c.name: c for c in community.channels}
    assert set(names) == {"general", "staff", "Lounge"}
    assert names["general"].category_id == store.vector_id("category", cat.id)
    assert names["staff"].private is True and names["general"].private is False
    assert names["Lounge"].view == "voice"
    assert community.everyone_perms != 0
    # the overwrite on #staff targets the cloned Mods role
    vmods = community.get_role(store.vector_id("role", mods.id))
    assert names["staff"].overwrites[vmods].view_channel is True
    # re-running doesn't duplicate anything
    await cloner.clone(g)
    assert len(community.channels) == 3 and len(community._roles) == 1

    # live sync: rename + delete → archive
    general.name = "chat"
    await cloner.sync_channel(general, community)
    assert names["general"].name == "chat"
    await cloner.archive_channel(general)
    assert names["general"].name == "archived-chat" and store.vector_id("channel", general.id) is None


class _Emoji:
    def __init__(self, name, id):
        self.name, self.id = name, id

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


# helpers ------------------------------------------------------------------------------------


def _async(value):
    async def fn(*a, **k):
        return value
    return fn


def _role(g, name, pos, *, default=False, managed=False, perms=None):
    ns = SimpleNamespace(id=f.snowflake(), name=name, position=pos, guild=g, managed=managed, hoist=False,
                         mentionable=False, permissions=perms or discord.Permissions.none(),
                         colour=discord.Colour(0x00FF00), is_default=lambda: default)
    return _RoleProxy(ns)


class _RoleProxy(discord.Role):
    """A discord.Role that skips the real constructor (isinstance checks pass)."""

    def __init__(self, ns):  # noqa: D401
        object.__setattr__(self, "_ns", ns)

    def __getattribute__(self, item):
        if item in ("_ns", "__class__", "__hash__", "__eq__"):
            return object.__getattribute__(self, item)
        return getattr(object.__getattribute__(self, "_ns"), item)

    def __hash__(self):
        return hash(object.__getattribute__(self, "_ns").id)

    def __eq__(self, other):
        return isinstance(other, _RoleProxy) and other.id == self.id


def _category(g, name, pos):
    from unittest.mock import MagicMock

    c = MagicMock(spec=discord.CategoryChannel)
    c.id, c.name, c.position, c.guild, c.overwrites = f.snowflake(), name, pos, g, {}
    return c


def _voice(g, name, pos):
    from unittest.mock import MagicMock

    c = MagicMock(spec=discord.VoiceChannel)
    c.id, c.name, c.position, c.guild, c.overwrites, c.category = f.snowflake(), name, pos, g, {}, None
    return c
