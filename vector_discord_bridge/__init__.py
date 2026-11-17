"""Clone a Discord server into a Vector Community and bridge the two so they act as one space."""

__version__ = "0.1.0"

from .config import Settings  # noqa: E402
from .store import Store  # noqa: E402

__all__ = ("Settings", "Store", "Bridge", "Cloner", "Relay")


def __getattr__(name: str):  # lazy: importing discord/vector is heavy
    if name == "Bridge":
        from .bridge import Bridge
        return Bridge
    if name == "Cloner":
        from .cloner import Cloner
        return Cloner
    if name == "Relay":
        from .relay import Relay
        return Relay
    raise AttributeError(name)
