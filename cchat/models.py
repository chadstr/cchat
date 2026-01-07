"""Data models shared between client and server."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def now_iso() -> str:
    return datetime.now().astimezone().strftime(ISO_FORMAT)


@dataclass
class Reaction:
    emoji: str
    user: str
    timestamp: str
    user_fingerprint: str | None = None


@dataclass
class ChatMessage:
    id: int
    user: str
    ciphertext: str
    timestamp: str
    reactions: List[Reaction] = field(default_factory=list)
    edited: bool = False
    decrypted_body: str | None = field(default=None, repr=False, compare=False)

    def to_payload(self) -> Dict:
        return {
            "id": self.id,
            "user": self.user,
            "ciphertext": self.ciphertext,
            "timestamp": self.timestamp,
            "reactions": [r.__dict__ for r in self.reactions],
            "edited": self.edited,
        }

    @classmethod
    def from_payload(cls, payload: Dict) -> "ChatMessage":
        reactions = [Reaction(**r) for r in payload.get("reactions", [])]
        return cls(
            id=payload["id"],
            user=payload["user"],
            ciphertext=payload["ciphertext"],
            timestamp=payload["timestamp"],
            reactions=reactions,
            edited=bool(payload.get("edited", False)),
        )
