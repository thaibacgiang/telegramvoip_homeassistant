from __future__ import annotations

import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class EventLog:
    def __init__(self, maxlen: int = 300) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def add(
        self,
        kind: str,
        message: str,
        level: str = "info",
        **extra: Any,
    ) -> dict[str, Any]:
        event = {
            "id": uuid.uuid4().hex[:10],
            "ts": utc_now(),
            "kind": kind,
            "level": level,
            "message": message,
        }
        event.update({key: value for key, value in extra.items() if value is not None})
        self._items.append(event)
        return event

    def snapshot(self, limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
        items = list(self._items)
        if kind:
            items = [item for item in items if item.get("kind") == kind]
        return list(reversed(items[-limit:]))
