"""Command line: ``vdbridge clone | bridge | run | invite | status | emojis``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional

from .config import Settings


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vdbridge", description="Clone a Discord server to Vector and bridge them.")
    p.add_argument("--env", default=".env", help="path to the .env file (default: .env)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def target(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--guild", type=int, help="Discord server ID (default: DISCORD_GUILD_ID)")
        sp.add_argument("--community", help="existing Vector Community ID to clone into (default: create one)")

    c = sub.add_parser("clone", help="clone only: copy the server into Vector and exit (nothing is bridged)")
    target(c)
    c.add_argument("--history", type=int, help="also copy the last N messages per channel")
    c.add_argument("--hidden", choices=("private", "overwrite", "skip"),
                   help="how to clone channels hidden from @everyone (default: private)")

    sub.add_parser("bridge", help="run the two-way bridge for every cloned server")

    r = sub.add_parser("run", help="clone (if not cloned yet) and then bridge")
    target(r)
    r.add_argument("--history", type=int)

    sub.add_parser("invite", help="print the Vector invite link for each bridged server")
    sub.add_parser("status", help="show what is linked")

    e = sub.add_parser("emojis", help="download the server's custom emoji for a Vector emoji pack")
    target(e)
    e.add_argument("--out", default="emojis")
    return p


async def _with_bridge(settings: Settings, job, *, relay: bool) -> int:
    from .bridge import Bridge

    bridge = Bridge(settings)
    try:
        await bridge.start(bridge=relay)
        return await job(bridge)
    finally:
        await bridge.close()


def _guild(bridge, guild_id: Optional[int]):
    gid = guild_id or bridge.settings.discord_guild_id
    if gid is None:
        raise SystemExit("no Discord server: pass --guild or set DISCORD_GUILD_ID")
    guild = bridge.discord.get_guild(gid)
    if guild is None:
        raise SystemExit(f"the Discord bot is not in server {gid}; invite it first (see README)")
    return guild


def main(argv: Optional[list] = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    settings = Settings.from_env(args.env)
    if getattr(args, "history", None) is not None:
        settings.clone_history = args.history
    if getattr(args, "hidden", None):
        settings.hidden_channels = args.hidden
    if args.cmd == "status":
        from .store import Store

        store = Store(settings.db_path)
        for gid, cid in store.links("guild"):
            print(f"Discord {gid} ⇄ Vector {cid}")
            for kind in ("role", "category", "channel"):
                print(f"  {kind}s: {len(store.links(kind, gid))}")
        return 0
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")

    async def clone(bridge) -> int:
        guild = _guild(bridge, args.guild)
        community = await bridge.cloner.clone(guild, args.community or settings.vector_community_id)
        url = await bridge.vector_invite(guild)
        print(f"\nCloned {guild.name} → Vector Community {community.id}\nInvite: {url}")
        if args.cmd == "clone":
            print("Clone only: nothing is bridged. Run `vdbridge bridge` later if you want the two joined.")
        return 0

    async def bridge_forever(bridge) -> int:
        if not bridge.store.links("guild"):
            raise SystemExit("nothing to bridge yet: run `vdbridge clone` first")
        done, _ = await asyncio.wait({bridge._vector_task, bridge._discord_task},
                                     return_when=asyncio.FIRST_COMPLETED)
        return 1 if any(t.exception() for t in done) else 0

    async def run(bridge) -> int:
        guild = _guild(bridge, args.guild)
        if bridge.store.vector_id("guild", guild.id) is None or args.community:
            await clone(bridge)
        return await bridge_forever(bridge)

    async def invite(bridge) -> int:
        for gid, _ in bridge.store.links("guild"):
            guild = bridge.discord.get_guild(gid)
            if guild:
                print(f"{guild.name}: {await bridge.vector_invite(guild)}")
        return 0

    async def emojis(bridge) -> int:
        from .cloner import Cloner

        guild = _guild(bridge, args.guild)
        n = await Cloner.download_emojis(guild, args.out)
        print(f"saved {n} emoji to {args.out}/{guild.id}; import them into a Vector emoji pack and set "
              f"VECTOR_EMOJI_PACK so :name: renders on both sides")
        return 0

    jobs = {"clone": clone, "bridge": bridge_forever, "run": run, "invite": invite, "emojis": emojis}
    try:
        return asyncio.run(_with_bridge(settings, jobs[args.cmd], relay=args.cmd in ("bridge", "run")))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
