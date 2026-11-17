"""Translate Discord permissions into Vector's permission bits.

Both libraries use the same permission *names* (vector-sdk-py follows discord.py), but the bit values differ, so we
go name by name. Discord permissions with no Vector equivalent are dropped. ``administrator`` grants every Vector bit.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from vector.permissions import ALL_BITS, VALID_FLAGS, _CANONICAL

#: Vector canonical name -> every name for that bit (Discord knows most of the same aliases).
_DISCORD_NAMES: Dict[str, Tuple[str, ...]] = {
    canon: tuple(n for n, b in VALID_FLAGS.items() if b == bit) for bit, canon in _CANONICAL.items()
}
# Discord split pinning out of manage_messages in 2025; anyone who can manage messages can pin.
_ALSO = {"pin_messages": ("manage_messages",)}


def _get(obj: Any, names: Tuple[str, ...]) -> Optional[bool]:
    for n in names:
        try:
            value = getattr(obj, n)
        except AttributeError:
            continue
        if value is not None:
            return bool(value)
    return None


def to_vector_bits(perms: Any) -> int:
    """A ``discord.Permissions`` → a Vector permission integer."""
    if getattr(perms, "administrator", False):
        return ALL_BITS
    bits = 0
    for canon, names in _DISCORD_NAMES.items():
        if _get(perms, names) or _get(perms, _ALSO.get(canon, ())):
            bits |= VALID_FLAGS[canon]
    return bits


def to_vector_overwrite(overwrite: Any) -> Dict[str, Optional[bool]]:
    """A ``discord.PermissionOverwrite`` → kwargs for ``vector.PermissionOverwrite``.

    Only explicitly set (allow/deny) values are copied; unset stays "inherit".
    """
    if getattr(overwrite, "administrator", None):
        return {canon: True for canon in _DISCORD_NAMES}
    out: Dict[str, Optional[bool]] = {}
    for canon, names in _DISCORD_NAMES.items():
        value = _get(overwrite, names)
        if value is None:
            value = _get(overwrite, _ALSO.get(canon, ()))
        if value is not None:
            out[canon] = value
    return out


def hides_from_everyone(overwrites: Dict[Any, Any], default_role: Any) -> bool:
    """True if the channel denies ``view_channel`` to @everyone (a Discord "private" channel)."""
    ow = overwrites.get(default_role)
    return bool(ow is not None and getattr(ow, "view_channel", None) is False)
