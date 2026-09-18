"""
Common interface both Brocade backends (SSH and REST) implement.

The sync/orchestrator code only ever depends on this abstract class, so
choosing SSH vs REST per-switch (config.yaml: `method: ssh` / `method: rest`)
is a pure implementation-swap with zero impact on the NetBox side.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.brocade.models import SwitchSnapshot


class BrocadeClientError(Exception):
    """Raised for any connection, auth, or parsing failure talking to a switch."""


class BrocadeClient(ABC):
    """Context-manager style client: connect on __enter__, clean up on __exit__."""

    def __enter__(self) -> "BrocadeClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    @abstractmethod
    def get_snapshot(self) -> SwitchSnapshot:
        """Fetch switch identity, all ports (with SFP info), and the full
        name server, and return them as one normalized SwitchSnapshot."""
        ...
