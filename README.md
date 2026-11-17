# vector-discord-bridge

Clone a Discord server into a [Vector](https://vectorapp.io) Community, then keep the two joined so they work as **one space**.
Messages, replies, attachments, edits and deletes flow both ways, and channel and role changes on Discord are mirrored to Vector.

Built on [`vector-sdk-py`](https://github.com/lucky-edgelord/vector-sdk-py) (Vector side) and `discord.py` (Discord side). The design draws on
[DiscordTool](https://github.com/gujarathisampath/DiscordTool) (cloning) and [Vecord](https://github.com/JSKitty/Vecord) (bridging).

```
 Discord server                         Vector Community
 ├─ Info/                    clone      ├─ Info/
 │  ├─ #general   ◄──────── bridge ───► │  ├─ #general
 │  └─ #staff (hidden)  ◄──────────────►│  └─ #staff (private)
 ├─ 🔊 Lounge   ◄──────────────────────►├─ 🔊 Lounge (text chat)
 └─ roles: Mods, Members …   ───sync──► └─ roles: Mods, Members …
```

## Two ways to use it

- **Clone only:** `vdbridge clone` copies the Discord server into a new Vector Community and exits. Nothing is relayed
  afterwards, no bot keeps running, and no `/vector` or `/discord` commands are added. Use this to move a community to Vector, or
  to make a one-time copy.
- **Clone + bridge:** `vdbridge run` (or `clone`, then `bridge`) keeps both sides joined as one space.

You can start with clone-only and add the bridge later. The clone's ID map (`bridge.db`) is all `vdbridge bridge` needs.

## What it does

**Clone** (`vdbridge clone`): copies the server's structure, with Discord as the source of truth.

| Discord | Vector |
|---|---|
| server name, description, icon, banner | Community name, description, icon, banner |
| roles: name, colour, hoist, mentionable, permissions, order | roles (bot/integration roles are skipped) |
| `@everyone` permissions | default permissions |
| categories, text/announcement, voice/stage, forum/media channels | categories, chat, voice, forum channels |
| role permission overwrites on categories and channels | overwrites (member-specific overwrites are skipped) |
| channels hidden from `@everyone` | encrypted **private** channels (configurable) |
| last *N* messages per channel (optional) | posted with the author's name and original time |
| custom emoji | downloaded to files for a Vector emoji pack (`vdbridge emojis`) |

Permissions are translated by name, because the two apps use different bit values. Discord-only permissions are dropped, and
`administrator` grants every Vector permission. Running the clone again updates what's already linked and never duplicates it.

**Bridge** (`vdbridge bridge`): one process runs both bots.

- Discord → Vector: the text is shown with a bold author line (Vector has no per-message names). Attachments are uploaded as real files and rich embeds are rendered.
  A reply to a bridged message becomes a native Vector reply. Thread and forum-post messages go to the parent channel, tagged `🧵 thread`.
- Vector → Discord: messages are posted through a webhook with the Vector user's **own name and avatar**. Attachments are uploaded. Replies show
  a quote with a jump link. `npub` mentions become `@names`, and `:emoji:` becomes the server's emoji. Forum channels receive a "Vector chat" post.
- Edits and deletes follow the message to the other side. Moderation carries over: deleting a bridged message on either side removes its twin.
- Loops can't happen, because the bridge ignores its own bot, its own webhooks and its own Vector key. `@everyone` and `@here` are never pinged.
- Live structure sync: creating, renaming or moving channels, categories and roles on Discord (including overwrites and server name or icon) updates
  Vector. A deleted Discord channel is **archived** on Vector (renamed `archived-…` and made read-only) rather than destroyed.
- `/vector` on Discord gives an invite to the Community. `/discord` (or `!discord`) on Vector gives an invite to the server.

## Setup

1. **Python 3.9+, Rust and cmake.** `vector-sdk-py` compiles the Rust Vector SDK on first install, which takes a few minutes.
   ```bash
   git clone https://github.com/lucky-edgelord/vector-discord-bridge && cd vector-discord-bridge
   python -m venv .venv && . .venv/bin/activate
   pip install -e .
   ```
2. **Discord bot.** In the [developer portal](https://discord.com/developers/applications), create an application and add a bot.
   Under *Bot*, enable the **Message Content Intent**. Then invite the bot with the `bot` and `applications.commands` scopes and these permissions:
   View Channels, Read Message History, Send Messages, Attach Files, **Manage Webhooks**, Manage Messages (to mirror Vector-side
   deletes), and Create Invite. Using a user token isn't supported: it breaks Discord's Terms of Service and gets accounts banned.
3. **Configure.** Copy `.env.example` to `.env` and set `DISCORD_TOKEN`, `DISCORD_GUILD_ID`, and optionally `VECTOR_NSEC`.
4. **Clone only**, or clone and bridge:
   ```bash
   vdbridge clone --history 50   # clone only: copies, prints the Vector invite link, exits
   vdbridge bridge               # optional: join the two (run under systemd / docker / tmux)
   # or clone + bridge in one go:
   vdbridge run
   ```
   For clone-only, the Discord bot needs just View Channels and Read Message History (plus Message Content Intent if you copy
   history). Manage Webhooks and the other permissions are only used by the bridge.

Other commands: `vdbridge status` shows what's linked, `vdbridge invite` prints the invite links, and `vdbridge emojis --out emojis` downloads the server's emoji.

When the bot creates the Community, the bridge's Vector key owns it. Give your own Vector account admin from inside Vector, or clone into a
Community you already own by setting `VECTOR_COMMUNITY_ID` (the bridge key must be an admin there).

### Emoji

Vector shows custom emoji from *emoji packs*, and the SDK can't create packs yet. To set them up:
1. Run `vdbridge emojis` to save the server's emoji as files.
2. Create a pack from those files in the Vector app.
3. Put its `naddr` in `VECTOR_EMOJI_PACK`.

After that, `:name:` renders on Vector, and Vector messages using `:name:` show the matching Discord emoji.

## Limits

- **Privacy:** anything said in a bridged Vector channel is copied to Discord's servers. Vector's encryption still protects the Vector
  side, but the Discord copy isn't private. Don't bridge channels whose members expect end-to-end privacy without telling them.
- **Private channels:** Vector private channels are readable only by members granted access. Grant Vector members access from the
  app (or with `channel.grant_access`). The bridge doesn't guess who should see them.
- **Structure flows one way:** changes made on Vector (new channels, renamed roles) aren't pushed back to Discord.
- **Not bridged:** reactions, polls, typing indicators, voice audio, member-specific overwrites, and stickers (shown as `[sticker: name]`).
- Cloned history gets new timestamps on Vector. The original time is shown in the author line.
- One Vector bot per process (an SDK rule). One bridge process can serve several Discord servers if you clone each one.

## Development

```bash
pip install -e ".[test]"
pytest            # uses in-memory fakes; no tokens or relays needed
```

Layout: `cloner.py` handles structure (clone and live sync), `relay.py` handles messages in both directions, `bridge.py` wires both bots' events,
`perms.py` translates permissions, `formatting.py` converts text, `store.py` is the SQLite ID map, and `__main__.py` is the CLI.

MIT licensed. Not affiliated with Discord or Vector.
