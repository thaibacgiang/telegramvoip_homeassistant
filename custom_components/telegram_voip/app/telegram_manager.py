from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ntgcalls import ConnectionNotFound
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.exceptions import (
    CallBusy,
    CallDeclined,
    CallDiscarded,
    NotInCallError,
    TimedOutAnswer,
)
from pytgcalls.types import CallConfig, ChatUpdate, StreamEnded
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.functions.contacts import ResolvePhoneRequest
from telethon.tl.types import User

from .const import VERSION
from .events import EventLog, utc_now
from .settings import AppSettings, mask_phone
from .store import AccountRecord, AccountStore, normalize_phone
from .tts import TtsService

_LOGGER = logging.getLogger(__name__)
_PHONE_RE = re.compile(r"^\+?[0-9]{8,15}$")
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(jpeg|jpg|png|webp);base64,([A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)
_MAX_PHOTO_BYTES = 8 * 1024 * 1024
_PENDING_TTL = 600


class TelegramError(RuntimeError):
    pass


class AccountUnavailable(TelegramError):
    pass


class RecipientError(TelegramError):
    pass


class LoginError(TelegramError):
    pass


@dataclass(slots=True)
class PendingLogin:
    id: str
    name: str
    phone: str
    api_id: int
    api_hash: str
    client: TelegramClient
    session_path: Path
    phone_code_hash: str
    created_at: float
    needs_password: bool = False


@dataclass(slots=True)
class AccountRuntime:
    record: AccountRecord
    client: TelegramClient
    calls: PyTgCalls
    me: dict[str, Any]
    state: str = "online"
    last_error: str | None = None
    call_events: dict[int, asyncio.Event] | None = None
    started_at: str | None = None

    def __post_init__(self) -> None:
        if self.call_events is None:
            self.call_events = {}
        if self.started_at is None:
            self.started_at = utc_now()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.record.id,
            "name": self.record.name,
            "phone": mask_phone(self.record.phone),
            "state": self.state,
            "me": self.me,
            "last_error": self.last_error,
            "started_at": self.started_at,
        }


@dataclass(slots=True)
class CallInfo:
    id: str
    account_id: str
    peer_id: int
    peer_name: str
    status: str = "queued"
    created_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    duration: float = 0.0
    kind: str = "call"
    ring_id: str | None = None
    task: asyncio.Task[Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "peer_id": self.peer_id,
            "peer_name": self.peer_name,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "duration": round(self.duration, 1),
            "kind": self.kind,
            "ring_id": self.ring_id,
        }


@dataclass(slots=True)
class RingInfo:
    id: str
    account_id: str
    peer_id: int
    peer_name: str
    status: str = "queued"
    attempts: int = 2
    attempt: int = 0
    created_at: str = ""
    ended_at: str | None = None
    call_id: str | None = None
    error: str | None = None
    task: asyncio.Task[Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "peer_id": self.peer_id,
            "peer_name": self.peer_name,
            "status": self.status,
            "attempts": self.attempts,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "call_id": self.call_id,
            "error": self.error,
        }


class TelegramManager:
    def __init__(
        self,
        settings: AppSettings,
        store: AccountStore,
        tts: TtsService,
        events: EventLog,
    ) -> None:
        self.settings = settings
        self.store = store
        self.tts = tts
        self.events = events
        self.runtimes: dict[str, AccountRuntime] = {}
        self.pending: dict[str, PendingLogin] = {}
        self.calls: dict[str, CallInfo] = {}
        self.rings: dict[str, RingInfo] = {}
        self.stats = {
            "messages_sent": 0,
            "voices_sent": 0,
            "photos_sent": 0,
            "calls_started": 0,
            "calls_answered": 0,
        }
        self.started_at = utc_now()

    async def start(self) -> None:
        for record in self.store.all():
            try:
                await self._start_runtime(record)
            except Exception as err:
                _LOGGER.warning("Cannot start account %s: %s", record.name, err)
                self.events.add(
                    "account",
                    f"Không khởi động được {record.name}: {err}",
                    level="error",
                    account_id=record.id,
                )
        self._expire_pending()

    async def close(self) -> None:
        for ring in list(self.rings.values()):
            await self.cancel_ring(ring.id)
        for call in list(self.calls.values()):
            if call.task and not call.task.done():
                call.task.cancel()
        for pending in list(self.pending.values()):
            await self._discard_pending(pending.id)
        for runtime in list(self.runtimes.values()):
            with suppress(Exception):
                await runtime.client.disconnect()
        self.runtimes.clear()

    def _new_client(self, api_id: int, api_hash: str, session_path: Path) -> TelegramClient:
        return TelegramClient(
            str(session_path),
            api_id,
            api_hash,
            device_model="Home Assistant Telegram VoIP",
            system_version="HA Add-on",
            app_version=VERSION,
            lang_code="vi",
            system_lang_code="vi",
        )

    async def _start_runtime(
        self,
        record: AccountRecord,
        client: TelegramClient | None = None,
    ) -> AccountRuntime:
        old = self.runtimes.pop(record.id, None)
        if old is not None and old.client is not client:
            with suppress(Exception):
                await old.client.disconnect()

        session_path = Path(record.session_file)
        if client is None and not session_path.is_file():
            raise AccountUnavailable("session file is missing; login again")

        resolved_client: TelegramClient = client or self._new_client(
            record.api_id, record.api_hash, session_path
        )
        client = resolved_client
        assert client is not None
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise AccountUnavailable("session is not authorized; login again")

        me = await client.get_me()
        call_py = PyTgCalls(client)
        runtime = AccountRuntime(
            record=record,
            client=client,
            calls=call_py,
            me={
                "id": me.id,
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
                "username": me.username or "",
            },
        )

        call_events = runtime.call_events
        assert call_events is not None

        async def on_stream_end(_client: PyTgCalls, update: Any) -> None:
            if isinstance(update, StreamEnded):
                event = call_events.get(update.chat_id)
                if event is not None:
                    event.set()

        async def on_chat_update(_client: PyTgCalls, update: Any) -> None:
            if (
                isinstance(update, ChatUpdate)
                and update.status & ChatUpdate.Status.DISCARDED_CALL
            ):
                event = call_events.get(update.chat_id)
                if event is not None:
                    event.set()

        call_py.on_update(fl.stream_end(StreamEnded.Type.AUDIO))(on_stream_end)
        call_py.on_update(fl.chat_update(ChatUpdate.Status.DISCARDED_CALL))(on_chat_update)
        await call_py.start()
        self.runtimes[record.id] = runtime
        self.events.add(
            "account",
            f"Đã kết nối {record.name}",
            account_id=record.id,
        )
        return runtime

    def _account_candidates(self, selector: str) -> list[AccountRecord]:
        phone = normalize_phone(selector)
        if not _PHONE_RE.fullmatch(phone):
            phone = ""
        folded = selector.casefold()
        matches: list[AccountRecord] = []
        for record in self.store.all():
            if record.id == selector:
                return [record]
            if (phone and normalize_phone(record.phone) == phone) or (
                record.name.casefold() == folded
            ):
                matches.append(record)
        return matches

    def runtime(self, account_id: str | None) -> AccountRuntime:
        selector = (account_id or "").strip()
        if not selector:
            for runtime in self.runtimes.values():
                if runtime.state == "online":
                    return runtime
            raise AccountUnavailable("no online Telegram account")

        direct = self.runtimes.get(selector)
        if direct is not None:
            if direct.state == "online":
                return direct
            raise AccountUnavailable(f"account is not online: {selector}")

        candidates = self._account_candidates(selector)
        if not candidates:
            raise AccountUnavailable(f"account not found: {selector}")
        online = [
            runtime
            for record in candidates
            if (runtime := self.runtimes.get(record.id)) is not None
            and runtime.state == "online"
        ]
        if not online:
            raise AccountUnavailable(f"account is not online: {selector}")
        if len(online) > 1:
            raise AccountUnavailable(
                f"account selector is ambiguous; use account ID: {selector}"
            )
        return online[0]

    def _api_credentials(
        self, api_id: int | None, api_hash: str | None
    ) -> tuple[int, str]:
        resolved_id = api_id or self.settings.api_id
        resolved_hash = (api_hash or self.settings.api_hash).strip()
        if not resolved_id or not resolved_hash:
            raise LoginError(
                "api_id/api_hash are required; create an app at my.telegram.org"
            )
        if len(resolved_hash) < 16:
            raise LoginError("api_hash looks invalid")
        return int(resolved_id), resolved_hash

    async def start_login(
        self,
        name: str,
        phone: str,
        api_id: int | None = None,
        api_hash: str | None = None,
    ) -> dict[str, Any]:
        self._expire_pending()
        clean_name = name.strip()
        clean_phone = normalize_phone(phone)
        if not clean_name:
            raise LoginError("name is required")
        if not _PHONE_RE.match(clean_phone):
            raise LoginError("phone number is invalid; use international format +84...")
        if self.store.find_by_phone(clean_phone):
            raise LoginError("this phone number is already added")
        resolved_id, resolved_hash = self._api_credentials(api_id, api_hash)

        login_id = uuid.uuid4().hex[:12]
        session_path = self.settings.sessions_dir / f"pending_{login_id}.session"
        client = self._new_client(resolved_id, resolved_hash, session_path)
        try:
            await client.connect()
            sent = await client.send_code_request(clean_phone)
        except FloodWaitError as err:
            await client.disconnect()
            session_path.unlink(missing_ok=True)
            raise LoginError(f"Telegram flood wait: {err.seconds}s") from err
        except PhoneNumberInvalidError as err:
            await client.disconnect()
            session_path.unlink(missing_ok=True)
            raise LoginError("Telegram rejected this phone number") from err
        except Exception as err:
            await client.disconnect()
            session_path.unlink(missing_ok=True)
            raise LoginError(str(err)) from err

        pending = PendingLogin(
            id=login_id,
            name=clean_name,
            phone=clean_phone,
            api_id=resolved_id,
            api_hash=resolved_hash,
            client=client,
            session_path=session_path,
            phone_code_hash=sent.phone_code_hash,
            created_at=time.time(),
        )
        self.pending[login_id] = pending
        self.events.add(
            "login",
            f"Đã gửi mã đăng nhập cho {mask_phone(clean_phone)}",
            account_id=login_id,
        )
        return {
            "login_id": login_id,
            "phone": mask_phone(clean_phone),
            "needs_password": False,
        }

    async def verify_login(
        self,
        login_id: str,
        code: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        pending = self.pending.get(login_id)
        if pending is None:
            raise LoginError("login session expired; request a new code")
        if time.time() - pending.created_at > _PENDING_TTL:
            await self._discard_pending(login_id)
            raise LoginError("login session expired; request a new code")

        try:
            if pending.needs_password:
                if not password:
                    return {"login_id": login_id, "needs_password": True}
                await pending.client.sign_in(password=password)
            else:
                if not code:
                    raise LoginError("code is required")
                await pending.client.sign_in(
                    phone=pending.phone,
                    code=code.strip(),
                    phone_code_hash=pending.phone_code_hash,
                )
        except SessionPasswordNeededError:
            pending.needs_password = True
            return {"login_id": login_id, "needs_password": True}
        except PasswordHashInvalidError as err:
            raise LoginError("2FA password is invalid") from err
        except PhoneCodeInvalidError as err:
            raise LoginError("login code is invalid") from err
        except PhoneCodeExpiredError as err:
            raise LoginError("login code expired") from err
        except FloodWaitError as err:
            raise LoginError(f"Telegram flood wait: {err.seconds}s") from err
        except Exception as err:
            raise LoginError(str(err)) from err

        me = await pending.client.get_me()
        account_id = uuid.uuid4().hex[:12]
        final_path = self.settings.sessions_dir / f"{account_id}.session"
        with suppress(Exception):
            await pending.client.disconnect()
        os.replace(pending.session_path, final_path)
        final_path.chmod(0o600)
        self.pending.pop(login_id, None)

        record = self.store.add(
            name=pending.name,
            phone=pending.phone,
            api_id=pending.api_id,
            api_hash=pending.api_hash,
            session_file=str(final_path),
            account_id=account_id,
        )
        try:
            await self._start_runtime(record)
        except Exception as err:
            self.events.add(
                "account",
                f"Đăng nhập xong nhưng chưa kết nối được: {err}",
                level="error",
                account_id=record.id,
            )
            raise LoginError(f"login succeeded but account is offline: {err}") from err

        self.events.add(
            "login",
            f"Đăng nhập thành công {record.name}",
            account_id=record.id,
        )
        return {
            "login_id": login_id,
            "needs_password": False,
            "account": {
                "id": record.id,
                "name": record.name,
                "phone": mask_phone(record.phone),
                "me": {
                    "id": me.id,
                    "first_name": me.first_name or "",
                    "last_name": me.last_name or "",
                    "username": me.username or "",
                },
            },
        }

    async def cancel_login(self, login_id: str) -> None:
        await self._discard_pending(login_id)

    async def _discard_pending(self, login_id: str) -> None:
        pending = self.pending.pop(login_id, None)
        if pending is None:
            return
        with suppress(Exception):
            await pending.client.disconnect()
        for item in self.settings.sessions_dir.glob(f"pending_{login_id}.session*"):
            item.unlink(missing_ok=True)

    def _expire_pending(self) -> None:
        expired = [
            pending
            for pending in self.pending.values()
            if time.time() - pending.created_at > _PENDING_TTL
        ]
        for pending in expired:
            asyncio.create_task(self._discard_pending(pending.id))

    async def logout(self, account_id: str) -> None:
        runtime = self.runtimes.get(account_id)
        for call in list(self.calls.values()):
            if call.account_id == account_id and call.task and not call.task.done():
                call.task.cancel()
        for ring in list(self.rings.values()):
            if ring.account_id == account_id:
                await self.cancel_ring(ring.id)
        if runtime is not None:
            with suppress(Exception):
                await runtime.client.log_out()
            with suppress(Exception):
                await runtime.client.disconnect()
            self.runtimes.pop(account_id, None)
        record = self.store.remove(account_id)
        if record is not None:
            for item in self.settings.sessions_dir.glob(f"{Path(record.session_file).stem}.session*"):
                item.unlink(missing_ok=True)
            self.events.add("account", f"Đã đăng xuất {record.name}", account_id=account_id)

    async def reconnect(self, account_id: str) -> dict[str, Any]:
        record = self.store.get(account_id)
        if record is None:
            raise AccountUnavailable("account not found")
        runtime = await self._start_runtime(record)
        return runtime.public()

    async def resolve_recipient(self, runtime: AccountRuntime, to: str) -> Any:
        value = to.strip()
        if not value:
            raise RecipientError("recipient is required")
        value = value.removeprefix("@")
        try:
            if _PHONE_RE.match(value) and (value.startswith("+") or len(value) >= 10):
                return await self._resolve_phone(runtime, value)
            if value.lstrip("-").isdigit():
                return await runtime.client.get_entity(int(value))
            return await runtime.client.get_entity(value)
        except RecipientError:
            raise
        except Exception as first_error:
            if _PHONE_RE.match(value):
                try:
                    return await self._resolve_phone(runtime, value)
                except Exception as phone_error:
                    raise RecipientError(
                        "cannot resolve recipient; use @username, user ID or a phone "
                        "number that is visible to this Telegram account"
                    ) from phone_error
            raise RecipientError(
                "cannot resolve recipient; use @username, user ID or a phone number"
            ) from first_error

    async def _resolve_phone(self, runtime: AccountRuntime, phone: str) -> Any:
        result = await runtime.client(ResolvePhoneRequest(phone=phone))
        users = getattr(result, "users", None) or []
        if not users:
            raise RecipientError(
                "phone number is not visible to this account; use @username or user ID"
            )
        return users[0]

    def peer_label(self, entity: Any) -> str:
        if isinstance(entity, User):
            parts = [entity.first_name or "", entity.last_name or ""]
            name = " ".join(part for part in parts if part).strip()
            if name:
                return name
            if entity.username:
                return f"@{entity.username}"
        name = getattr(entity, "title", "") or getattr(entity, "username", "")
        return str(name or getattr(entity, "id", "unknown"))

    async def send_message(
        self,
        account_id: str | None,
        to: str,
        message: str,
        parse_mode: str | None = None,
        silent: bool = False,
    ) -> dict[str, Any]:
        runtime = self.runtime(account_id)
        entity = await self.resolve_recipient(runtime, to)
        mode = (parse_mode or "").strip().lower() or None
        if mode == "plain":
            mode = None
        result = await runtime.client.send_message(
            entity,
            message,
            parse_mode=mode,
            silent=silent,
        )
        self.stats["messages_sent"] += 1
        self.events.add(
            "message",
            f"Đã gửi tin nhắn đến {self.peer_label(entity)}",
            account_id=runtime.record.id,
            peer_id=getattr(entity, "id", None),
        )
        return {
            "message_id": result.id,
            "peer_id": getattr(entity, "id", None),
            "peer_name": self.peer_label(entity),
        }

    async def send_photo(
        self,
        account_id: str | None,
        to: str,
        image: str,
        caption: str | None = None,
        silent: bool = False,
    ) -> dict[str, Any]:
        runtime = self.runtime(account_id)
        entity = await self.resolve_recipient(runtime, to)
        target = image.strip()
        temp_path: Path | None = None
        try:
            if target.startswith("data:"):
                match = _DATA_IMAGE_RE.fullmatch(target)
                if not match:
                    raise TelegramError("Ảnh tải lên không hợp lệ")
                try:
                    raw = base64.b64decode(match.group(2), validate=True)
                except (binascii.Error, ValueError) as err:
                    raise TelegramError("Ảnh tải lên không hợp lệ") from err
                if not raw or len(raw) > _MAX_PHOTO_BYTES:
                    raise TelegramError("Ảnh phải nhỏ hơn 8 MB")
                suffix = ".jpg" if match.group(1).lower() in {"jpeg", "jpg"} else (
                    f".{match.group(1).lower()}"
                )
                directory = self.settings.cache_dir / "outgoing"
                directory.mkdir(parents=True, exist_ok=True)
                temp_path = directory / f"photo-{uuid.uuid4().hex}{suffix}"
                temp_path.write_bytes(raw)
                target = str(temp_path)
            elif re.match(r"^https?://", target, re.IGNORECASE):
                if len(target) > 2048:
                    raise TelegramError("URL ảnh quá dài")
            else:
                media_root = (self.settings.data_dir / "media").resolve()
                path = Path(target).expanduser()
                resolved = (path if path.is_absolute() else media_root / path).resolve()
                try:
                    resolved.relative_to(media_root)
                except ValueError as err:
                    raise TelegramError(
                        "Ảnh phải là URL http(s), dữ liệu tải lên hoặc file trong /data/media"
                    ) from err
                if not resolved.is_file():
                    raise TelegramError("Không tìm thấy file ảnh")
                target = str(resolved)

            result = await runtime.client.send_file(
                entity,
                target,
                caption=(caption or "").strip() or None,
                silent=silent,
            )
        finally:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink()

        self.stats["photos_sent"] += 1
        self.events.add(
            "photo",
            f"Đã gửi ảnh đến {self.peer_label(entity)}",
            account_id=runtime.record.id,
            peer_id=getattr(entity, "id", None),
        )
        return {
            "message_id": result.id,
            "peer_id": getattr(entity, "id", None),
            "peer_name": self.peer_label(entity),
        }

    async def send_tts(
        self,
        account_id: str | None,
        to: str,
        text: str,
        voice: str | None = None,
        rate: str | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        runtime = self.runtime(account_id)
        entity = await self.resolve_recipient(runtime, to)
        voice = self.tts.resolve_voice(voice)
        rate = self.tts.resolve_rate(rate)
        audio = await self.tts.synthesize(text, voice, rate)
        result = await runtime.client.send_file(
            entity,
            str(audio),
            voice_note=True,
            caption=(caption or None),
        )
        self.stats["voices_sent"] += 1
        self.events.add(
            "voice",
            f"Đã gửi voice TTS ({voice}) đến {self.peer_label(entity)}",
            account_id=runtime.record.id,
            peer_id=getattr(entity, "id", None),
        )
        return {
            "message_id": result.id,
            "peer_id": getattr(entity, "id", None),
            "peer_name": self.peer_label(entity),
            "voice": voice,
            "rate": rate,
        }

    async def list_dialogs(self, account_id: str, limit: int = 80) -> list[dict[str, Any]]:
        runtime = self.runtime(account_id)
        dialogs = await runtime.client.get_dialogs(limit=max(1, min(limit, 200)))
        items: list[dict[str, Any]] = []
        for dialog in dialogs:
            entity = dialog.entity
            if not isinstance(entity, User):
                continue
            items.append(
                {
                    "id": entity.id,
                    "name": self.peer_label(entity),
                    "username": entity.username or "",
                    "is_bot": bool(entity.bot),
                    "deleted": bool(entity.deleted),
                }
            )
        return items

    async def start_call(
        self,
        account_id: str | None,
        to: str,
        text: str,
        voice: str | None = None,
        rate: str | None = None,
        ring_timeout: int | None = None,
        audio_path: Path | None = None,
        kind: str = "call",
        ring_id: str | None = None,
        resolved_entity: Any | None = None,
    ) -> CallInfo:
        runtime = self.runtime(account_id)
        # Chuông báo đã resolve người nhận ở start_ring; tái sử dụng entity
        # để tránh Telethon get_entity(peer_id) thất bại do thiếu access_hash.
        entity = resolved_entity or await self.resolve_recipient(runtime, to)
        if not isinstance(entity, User):
            raise RecipientError("private calls require a Telegram user, not a group")
        peer_id = entity.id
        if peer_id <= 0:
            raise RecipientError("invalid recipient")
        for call in self.calls.values():
            if (
                call.account_id == runtime.record.id
                and call.peer_id == peer_id
                and call.status in {"queued", "ringing", "connected"}
            ):
                raise TelegramError("a call to this recipient is already active")

        audio = audio_path or await self.tts.synthesize(text, voice, rate)
        duration = await self.tts.duration(audio)
        timeout = ring_timeout or self.settings.ring_timeout
        event = asyncio.Event()
        assert runtime.call_events is not None
        runtime.call_events[peer_id] = event

        info = CallInfo(
            id=uuid.uuid4().hex[:12],
            account_id=runtime.record.id,
            peer_id=peer_id,
            peer_name=self.peer_label(entity),
            created_at=utc_now(),
            duration=duration,
            kind=kind,
            ring_id=ring_id,
        )
        info.task = asyncio.create_task(
            self._run_call(
                info,
                runtime,
                event,
                audio,
                timeout,
            )
        )
        self.calls[info.id] = info
        self.stats["calls_started"] += 1
        self.events.add(
            "call",
            f"Bắt đầu gọi {info.peer_name}",
            account_id=runtime.record.id,
            call_id=info.id,
            ring_id=ring_id,
        )
        return info

    async def _run_call(
        self,
        info: CallInfo,
        runtime: AccountRuntime,
        event: asyncio.Event,
        audio: Path,
        timeout: int,
    ) -> None:
        assert runtime.call_events is not None
        try:
            info.status = "ringing"
            await runtime.calls.play(
                info.peer_id,
                str(audio),
                CallConfig(timeout=timeout),
            )
            info.status = "connected"
            info.started_at = utc_now()
            self.stats["calls_answered"] += 1
            self.events.add(
                "call",
                f"{info.peer_name} đã nghe máy",
                account_id=info.account_id,
                call_id=info.id,
            )
            wait_seconds = min(info.duration + 12, self.settings.max_call_seconds)
            with suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
            info.status = "completed"
        except asyncio.CancelledError:
            info.status = "cancelled"
            raise
        except TimedOutAnswer:
            info.status = "no_answer"
            info.error = "ring timeout"
        except CallDeclined:
            info.status = "declined"
            info.error = "recipient declined"
        except CallBusy:
            info.status = "busy"
            info.error = "recipient is busy"
        except CallDiscarded:
            info.status = "cancelled"
            info.error = "call discarded"
        except Exception as err:
            info.status = "failed"
            info.error = str(err)
            _LOGGER.warning("Call %s failed: %s", info.id, err)
        finally:
            try:
                await runtime.calls.leave_call(info.peer_id)
            except (NotInCallError, ConnectionNotFound):
                pass
            except Exception as err:
                _LOGGER.debug("leave_call failed: %s", err)
            runtime.call_events.pop(info.peer_id, None)
            info.ended_at = utc_now()
            self.events.add(
                "call",
                f"Kết thúc gọi {info.peer_name}: {info.status}",
                level="error" if info.status in {"failed", "busy"} else "info",
                account_id=info.account_id,
                call_id=info.id,
                ring_id=info.ring_id,
            )

    async def start_ring(
        self,
        account_id: str | None,
        to: str,
        text: str,
        voice: str | None = None,
        rate: str | None = None,
        attempts: int | None = None,
        interval: int | None = None,
        ring_timeout: int | None = None,
        send_message: bool = True,
    ) -> RingInfo:
        runtime = self.runtime(account_id)
        entity = await self.resolve_recipient(runtime, to)
        if not isinstance(entity, User):
            raise RecipientError("ring requires a Telegram user")
        audio = await self.tts.synthesize(text, voice, rate)
        info = RingInfo(
            id=uuid.uuid4().hex[:12],
            account_id=runtime.record.id,
            peer_id=entity.id,
            peer_name=self.peer_label(entity),
            attempts=max(1, min(attempts or self.settings.ring_attempts, 10)),
            created_at=utc_now(),
        )
        info.task = asyncio.create_task(
            self._run_ring(
                info,
                runtime,
                audio,
                max(0, min(interval if interval is not None else self.settings.ring_interval, 600)),
                ring_timeout or self.settings.ring_timeout,
                send_message,
                text,
                voice,
                rate,
                entity,
            )
        )
        self.rings[info.id] = info
        self.events.add(
            "ring",
            f"Bắt đầu chuông báo cho {info.peer_name}",
            account_id=runtime.record.id,
            ring_id=info.id,
        )
        return info

    async def _run_ring(
        self,
        ring: RingInfo,
        runtime: AccountRuntime,
        audio: Path,
        interval: int,
        ring_timeout: int,
        send_message: bool,
        text: str,
        voice: str | None,
        rate: str | None,
        entity: User,
    ) -> None:
        current_call: CallInfo | None = None
        try:
            ring.status = "running"
            if send_message:
                try:
                    # Dùng entity đã resolve ở start_ring, không resolve lại bằng
                    # numeric ID (Telethon có thể không có access_hash trong cache).
                    await runtime.client.send_message(entity, text)
                    self.stats["messages_sent"] += 1
                except Exception as err:
                    _LOGGER.warning("Ring message failed: %s", err)
            for attempt in range(1, ring.attempts + 1):
                ring.attempt = attempt
                current_call = await self.start_call(
                    account_id=ring.account_id,
                    to=str(ring.peer_id),
                    text=text,
                    voice=voice,
                    rate=rate,
                    ring_timeout=ring_timeout,
                    audio_path=audio,
                    kind="ring",
                    ring_id=ring.id,
                    resolved_entity=entity,
                )
                ring.call_id = current_call.id
                assert current_call.task is not None
                await current_call.task
                if current_call.status in {"completed", "connected"}:
                    ring.status = "answered"
                    break
                if attempt < ring.attempts:
                    await asyncio.sleep(interval)
            if ring.status == "running":
                ring.status = "no_answer"
        except asyncio.CancelledError:
            ring.status = "cancelled"
            if current_call and current_call.task and not current_call.task.done():
                current_call.task.cancel()
            raise
        except Exception as err:
            ring.status = "failed"
            ring.error = str(err)
            _LOGGER.warning("Ring %s failed: %s", ring.id, err)
        finally:
            ring.ended_at = utc_now()
            self.events.add(
                "ring",
                f"Kết thúc chuông báo {ring.peer_name}: {ring.status}",
                level="error" if ring.status == "failed" else "info",
                account_id=ring.account_id,
                ring_id=ring.id,
            )

    async def hangup(self, call_id: str | None = None, account_id: str | None = None) -> int:
        target_account = None
        if account_id:
            selector = account_id.strip()
            candidates = [selector] if selector in self.runtimes else []
            if not candidates:
                candidates = [record.id for record in self._account_candidates(selector)]
            if not candidates:
                raise AccountUnavailable(f"account not found: {selector}")
            if len(candidates) > 1:
                raise AccountUnavailable(
                    f"account selector is ambiguous; use account ID: {selector}"
                )
            target_account = candidates[0]
        targets = []
        for call in self.calls.values():
            if call_id and call.id != call_id:
                continue
            if target_account and call.account_id != target_account:
                continue
            if call.status in {"queued", "ringing", "connected"}:
                targets.append(call)
        for call in targets:
            call.status = "cancelled"
            if call.task and not call.task.done():
                call.task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await call.task
        return len(targets)

    async def cancel_ring(self, ring_id: str) -> None:
        ring = self.rings.get(ring_id)
        if ring is None:
            raise TelegramError("ring not found")
        ring.status = "cancelled"
        if ring.task and not ring.task.done():
            ring.task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await ring.task

    def public_calls(self) -> list[dict[str, Any]]:
        items = sorted(
            self.calls.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )
        return [item.public() for item in items]

    def public_rings(self) -> list[dict[str, Any]]:
        items = sorted(
            self.rings.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )
        return [item.public() for item in items]

    def accounts(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for record in self.store.all():
            runtime = self.runtimes.get(record.id)
            if runtime is not None:
                result.append(runtime.public())
            else:
                result.append(
                    {
                        "id": record.id,
                        "name": record.name,
                        "phone": mask_phone(record.phone),
                        "state": "offline",
                        "me": None,
                        "last_error": None,
                        "started_at": None,
                    }
                )
        return result

    def status(self) -> dict[str, Any]:
        accounts = self.accounts()
        calls = self.public_calls()
        return {
            "version": VERSION,
            "started_at": self.started_at,
            "accounts": accounts,
            "accounts_online": sum(1 for item in accounts if item["state"] == "online"),
            "calls": calls,
            "active_calls": sum(
                1 for item in calls if item["status"] in {"queued", "ringing", "connected"}
            ),
            "rings": self.public_rings(),
            "stats": dict(self.stats),
            "tts": self.tts.stats(),
        }
