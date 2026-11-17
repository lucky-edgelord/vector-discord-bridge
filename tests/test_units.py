import discord
import pytest
from vector.permissions import ALL_BITS, VALID_FLAGS

from vector_discord_bridge import formatting as fmt
from vector_discord_bridge.perms import hides_from_everyone, to_vector_bits, to_vector_overwrite
from vector_discord_bridge.relay import split_header
from vector_discord_bridge.store import Store

NPUB = "npub1" + "a" * 58


def test_to_vector_header_emoji_thread_and_stamp():
    out = fmt.to_vector("hi <:wave:123> <a:dance:9>", "Ana", thread="ideas", stamp=1700000000)
    assert out == "**Ana** · 🧵 ideas · <t:1700000000:f>\nhi :wave: :dance:"
    assert split_header(out) == ("Ana", "hi :wave: :dance:")


def test_to_vector_reply_quote_and_extras():
    out = fmt.to_vector("yes", "Bo", quote="> ↪ **Ana**: q", extras=["[sticker: cat]"])
    assert out == "**Bo**\n> ↪ **Ana**: q\nyes\n[sticker: cat]"


def test_to_discord_mentions_emoji_everyone():
    out = fmt.to_discord(f"hey nostr:{NPUB} :wave: :nope: @everyone",
                         resolve_npub=lambda n: "Ana", emojis={"wave": "<:wave:1>"})
    assert out == "hey @Ana <:wave:1> :nope: @​everyone"
    assert fmt.to_discord(NPUB).startswith("@npub1aaaa")


def test_split_and_webhook_name():
    parts = fmt.split_discord("a" * 1500 + "\n" + "b" * 1500)
    assert [len(p) for p in parts] == [1500, 1500]
    assert all(len(p) <= 2000 for p in fmt.split_discord("x" * 5000))
    name = fmt.webhook_username("My Discord Fan", " (Vector)")
    assert "discord" not in name.lower() and name.endswith(" (Vector)")
    assert len(fmt.webhook_username("n" * 200)) == 80


def test_permissions_map_by_name():
    bits = to_vector_bits(discord.Permissions(send_messages=True, view_channel=True, kick_members=True))
    assert bits == VALID_FLAGS["send_messages"] | VALID_FLAGS["view_channel"] | VALID_FLAGS["kick_members"]
    assert to_vector_bits(discord.Permissions(administrator=True)) == ALL_BITS
    # older guilds: pin follows manage_messages
    assert to_vector_bits(discord.Permissions(manage_messages=True)) & VALID_FLAGS["pin_messages"]


def test_overwrite_and_hidden():
    ow = discord.PermissionOverwrite(view_channel=False, send_messages=True)
    assert to_vector_overwrite(ow) == {"view_channel": False, "send_messages": True}
    everyone = object()
    assert hides_from_everyone({everyone: ow}, everyone)
    assert not hides_from_everyone({}, everyone)


def test_store_roundtrip(tmp_path):
    s = Store(str(tmp_path / "b.db"))
    s.link("channel", 5, "v5", 1)
    assert s.vector_id("channel", 5) == "v5" and s.discord_id("channel", "v5") == 5
    s.link("channel", 5, "v6", 1)
    assert s.vector_id("channel", 5) == "v6"
    s.add_message(10, "m1", 5, "v6", "discord")
    s.add_message(10, "m2", 5, "v6", "discord")
    assert [l.vector_id for l in s.by_discord(10)] == ["m1", "m2"]
    s.forget_discord(10)
    assert s.by_discord(10) == []
    s.set_webhook(5, 99, "t")
    assert s.webhook(5) == (99, "t") and list(s.webhook_ids()) == [99]


async def test_bridge_wires_both_bots(tmp_path):
    from vector_discord_bridge.bridge import Bridge
    from vector_discord_bridge.config import Settings

    b = Bridge(Settings(discord_token="x", db_path=str(tmp_path / "b.db"), vector_data_dir=str(tmp_path / "v")))
    b.install()
    assert b.tree.get_command("vector") is not None
    assert b.vector.get_command("discord") is not None
    assert {"on_message", "on_raw_message_edit", "on_raw_message_delete"} <= set(b.vector.extra_events)
    for ev in ("on_message", "on_guild_channel_update", "on_guild_role_delete", "on_raw_message_delete"):
        assert hasattr(b.discord, ev)
    b.store.close()


async def test_clone_only_installs_no_relay(tmp_path, monkeypatch):
    from vector_discord_bridge.bridge import Bridge
    from vector_discord_bridge.config import Settings

    b = Bridge(Settings(discord_token="x", db_path=str(tmp_path / "b.db"), vector_data_dir=str(tmp_path / "v")))

    async def noop(*a, **k):
        return None

    for client in (b.vector, b.discord):
        monkeypatch.setattr(client, "login", noop)
        monkeypatch.setattr(client, "connect", noop)
        monkeypatch.setattr(client, "wait_until_ready", noop)
    await b.start(bridge=False)
    assert not b._installed
    assert "on_message" not in b.vector.extra_events and b.tree.get_command("vector") is None
    b.store.close()
