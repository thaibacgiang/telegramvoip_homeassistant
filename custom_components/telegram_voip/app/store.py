from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .settings import mask_phone


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class AccountRecord:
    id: str
    name: str
    phone: str
    api_id: int
    api_hash: str
    session_file: str
    created_at: str = field(default_factory=utc_now)
    last_login_at: str = field(default_factory=utc_now)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "phone": mask_phone(self.phone),
            "api_id": self.api_id,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }


class AccountStore:
    def __init__(self, path: Path, sessions_dir: Path) -> None:
        self.path = path
        self.sessions_dir = sessions_dir
        self._lock = threading.RLock()
        self._accounts: dict[str, AccountRecord] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            self._accounts = {}
            if not self.path.is_file():
                return
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            items = payload.get("accounts", []) if isinstance(payload, dict) else []
            for item in items:
                try:
                    record = AccountRecord(
                        id=str(item["id"]),
                        name=str(item["name"]),
                        phone=str(item["phone"]),
                        api_id=int(item["api_id"]),
                        api_hash=str(item["api_hash"]),
                        session_file=str(item["session_file"]),
                        created_at=str(item.get("created_at") or utc_now()),
                        last_login_at=str(item.get("last_login_at") or utc_now()),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                self._accounts[record.id] = record

    def _save(self) -> None:
        payload = {"accounts": [asdict(record) for record in self._accounts.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".accounts-", suffix=".json", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def all(self) -> list[AccountRecord]:
        with self._lock:
            return list(self._accounts.values())

    def get(self, account_id: str) -> AccountRecord | None:
        with self._lock:
            return self._accounts.get(account_id)

    def find_by_phone(self, phone: str) -> AccountRecord | None:
        normalized = normalize_phone(phone)
        with self._lock:
            for record in self._accounts.values():
                if normalize_phone(record.phone) == normalized:
                    return record
        return None

    def add(
        self,
        name: str,
        phone: str,
        api_id: int,
        api_hash: str,
        session_file: str,
        account_id: str | None = None,
    ) -> AccountRecord:
        with self._lock:
            record = AccountRecord(
                id=account_id or uuid.uuid4().hex[:12],
                name=name.strip(),
                phone=normalize_phone(phone),
                api_id=api_id,
                api_hash=api_hash,
                session_file=session_file,
            )
            self._accounts[record.id] = record
            self._save()
            return record

    def update_login(self, account_id: str, session_file: str) -> AccountRecord:
        with self._lock:
            record = self._accounts[account_id]
            record.session_file = session_file
            record.last_login_at = utc_now()
            self._save()
            return record

    def remove(self, account_id: str) -> AccountRecord | None:
        with self._lock:
            record = self._accounts.pop(account_id, None)
            if record is not None:
                self._save()
            return record


def normalize_phone(phone: str) -> str:
    value = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if value and not value.startswith("+"):
        value = "+" + value.lstrip("+")
    return value
