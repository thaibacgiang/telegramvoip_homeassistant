from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
import socket
from contextlib import asynccontextmanager, suppress
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .const import API_PORT, VERSION, VOICES, WYOMING_VOICES
from .events import EventLog
from .schemas import (
    CallRequest,
    CredentialsUpdateRequest,
    HangupRequest,
    LoginStartRequest,
    LoginVerifyRequest,
    MessageRequest,
    PhotoRequest,
    RingRequest,
    TtsRequest,
)
from .settings import AppSettings, env_file_path, load_settings, save_env_values
from .store import AccountStore
from .telegram_manager import (
    AccountUnavailable,
    LoginError,
    RecipientError,
    TelegramError,
    TelegramManager,
)
from .tts import TtsError, TtsService

_LOGGER = logging.getLogger(__name__)
_UI_DIR = Path(__file__).resolve().parent / "ui"


async def require_auth(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_api_token: Annotated[str | None, Header(alias="X-API-Token")] = None,
) -> None:
    supplied = (x_api_token or "").strip()
    if not supplied and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied = value.strip()
    expected = request.app.state.settings.api_token
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _manager(request: Request) -> TelegramManager:
    return request.app.state.manager

_INGRESS_NETWORK = ipaddress.ip_network("172.30.32.0/23")

@lru_cache(maxsize=1)
def _supervisor_ip() -> str | None:
    try:
        return socket.gethostbyname("supervisor")
    except OSError:
        return None

def _is_ingress_request(request: Request) -> bool:
    if not request.headers.get("X-Ingress-Path"):
        return False
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in _INGRESS_NETWORK or host == _supervisor_ip()


def create_app(
    settings: AppSettings | None = None,
    manager: TelegramManager | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_services = getattr(app.state, "manager", None) is None
        if own_services:
            loaded = settings or load_settings()
            store = AccountStore(loaded.accounts_file, loaded.sessions_dir)
            events = EventLog(loaded.log_limit)
            tts = TtsService(
                loaded.tts_cache_dir,
                default_voice=loaded.tts_voice,
                default_rate=loaded.tts_rate,
                engine=loaded.tts_engine,
                wyoming_host=loaded.wyoming_host,
                wyoming_port=loaded.wyoming_port,
                wyoming_voice=loaded.wyoming_voice,
                fallback_edge=loaded.tts_fallback_edge,
            )
            manager_instance = TelegramManager(loaded, store, tts, events)
            app.state.settings = loaded
            app.state.events = events
            app.state.tts = tts
            app.state.manager = manager_instance
            await manager_instance.start()
        else:
            app.state.events = getattr(app.state, "events", EventLog())
            app.state.tts = getattr(app.state, "tts", None)

        async def pending_loop() -> None:
            while True:
                await asyncio.sleep(60)
                try:
                    app.state.manager._expire_pending()
                except Exception:
                    _LOGGER.exception("Pending login cleanup failed")

        cleanup = asyncio.create_task(pending_loop())
        try:
            yield
        finally:
            cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup
            if own_services:
                await app.state.manager.close()

    app = FastAPI(
        title="Telegram VoIP Notifier",
        version=VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if settings is not None:
        app.state.settings = settings
    if manager is not None:
        app.state.manager = manager

    @app.exception_handler(LoginError)
    async def login_error_handler(_request: Request, err: LoginError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(err)})

    @app.exception_handler(RecipientError)
    async def recipient_error_handler(_request: Request, err: RecipientError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(err)})

    @app.exception_handler(AccountUnavailable)
    async def account_error_handler(_request: Request, err: AccountUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(err)})

    @app.exception_handler(TtsError)
    async def tts_error_handler(_request: Request, err: TtsError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(err)})

    @app.exception_handler(TelegramError)
    async def telegram_error_handler(_request: Request, err: TelegramError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(err)})

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": VERSION}

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        html = (_UI_DIR / "index.html").read_text(encoding="utf-8")
        if _is_ingress_request(request):
            token = request.app.state.settings.api_token
            token_json = (
                json.dumps(token)
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
            )
            html = html.replace(
                "window.__TGVOIP_TOKEN__ = null;",
                f"window.__TGVOIP_TOKEN__ = {token_json};",
                1,
            )
        return Response(
            html,
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/styles.css", include_in_schema=False)
    async def styles() -> FileResponse:
        return FileResponse(_UI_DIR / "styles.css", media_type="text/css")

    @app.get("/app.js", include_in_schema=False)
    async def app_js() -> FileResponse:
        return FileResponse(_UI_DIR / "app.js", media_type="text/javascript")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/config", dependencies=[Depends(require_auth)])
    async def config(request: Request) -> dict[str, Any]:
        cfg: AppSettings = request.app.state.settings
        return {
            "version": VERSION,
            "voices": list(WYOMING_VOICES if cfg.tts_engine == "wyoming" else VOICES),
            "tts_engine": cfg.tts_engine,
            "api_id": cfg.api_id,
            "api_configured": bool(cfg.api_id and cfg.api_hash),
            "api_hash_configured": bool(cfg.api_hash),
            "defaults": {
                "voice": cfg.wyoming_voice if cfg.tts_engine == "wyoming" else cfg.tts_voice,
                "rate": cfg.tts_rate,
                "ring_timeout": cfg.ring_timeout,
                "ring_attempts": cfg.ring_attempts,
                "ring_interval": cfg.ring_interval,
            },
        }

    @app.post("/api/settings/credentials", dependencies=[Depends(require_auth)])
    async def update_credentials(
        request: Request,
        body: CredentialsUpdateRequest,
    ) -> dict[str, Any]:
        cfg: AppSettings = request.app.state.settings
        updates: dict[str, str] = {}
        if body.api_id is not None:
            updates["TGVOIP_API_ID"] = str(body.api_id)
        if body.api_hash is not None:
            api_hash = body.api_hash.strip()
            if len(api_hash) < 16:
                raise HTTPException(status_code=400, detail="api_hash looks invalid")
            updates["TGVOIP_API_HASH"] = api_hash
        token = body.api_token.strip() if body.api_token is not None else ""
        if token:
            updates["TGVOIP_API_TOKEN"] = token

        if updates:
            try:
                save_env_values(env_file_path(cfg.data_dir), updates)
            except OSError as err:
                raise HTTPException(
                    status_code=500,
                    detail=f"cannot save credentials: {err}",
                ) from err
        if body.api_id is not None:
            cfg.api_id = body.api_id
        if body.api_hash is not None:
            cfg.api_hash = body.api_hash.strip()
        if token:
            cfg.api_token = token
            try:
                cfg.api_token_file.write_text(token + "\n", encoding="utf-8")
                cfg.api_token_file.chmod(0o600)
            except OSError as err:
                raise HTTPException(
                    status_code=500,
                    detail=f"cannot save API token: {err}",
                ) from err

        return {
            "api_id": cfg.api_id,
            "api_hash_configured": bool(cfg.api_hash),
            "api_token": cfg.api_token,
        }

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    async def status(request: Request) -> dict[str, Any]:
        return _manager(request).status()

    @app.get("/api/accounts", dependencies=[Depends(require_auth)])
    async def accounts(request: Request) -> dict[str, Any]:
        return {"accounts": _manager(request).accounts()}

    @app.post("/api/accounts/login", dependencies=[Depends(require_auth)])
    async def login_start(request: Request, body: LoginStartRequest) -> dict[str, Any]:
        return await _manager(request).start_login(
            name=body.name,
            phone=body.phone,
            api_id=body.api_id,
            api_hash=body.api_hash,
        )

    @app.post("/api/accounts/login/{login_id}/verify", dependencies=[Depends(require_auth)])
    async def login_verify(
        request: Request,
        login_id: str,
        body: LoginVerifyRequest,
    ) -> dict[str, Any]:
        return await _manager(request).verify_login(
            login_id=login_id,
            code=body.code,
            password=body.password,
        )

    @app.delete("/api/accounts/login/{login_id}", dependencies=[Depends(require_auth)])
    async def login_cancel(request: Request, login_id: str) -> dict[str, bool]:
        await _manager(request).cancel_login(login_id)
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/reconnect", dependencies=[Depends(require_auth)])
    async def account_reconnect(request: Request, account_id: str) -> dict[str, Any]:
        return await _manager(request).reconnect(account_id)

    @app.delete("/api/accounts/{account_id}", dependencies=[Depends(require_auth)])
    async def account_delete(request: Request, account_id: str) -> dict[str, bool]:
        await _manager(request).logout(account_id)
        return {"ok": True}

    @app.get("/api/accounts/{account_id}/dialogs", dependencies=[Depends(require_auth)])
    async def dialogs(
        request: Request,
        account_id: str,
        limit: int = Query(default=80, ge=1, le=200),
    ) -> dict[str, Any]:
        return {"dialogs": await _manager(request).list_dialogs(account_id, limit)}

    @app.post("/api/messages", dependencies=[Depends(require_auth)])
    async def send_message(request: Request, body: MessageRequest) -> dict[str, Any]:
        return await _manager(request).send_message(
            account_id=body.account,
            to=body.to,
            message=body.message,
            parse_mode=body.parse_mode,
            silent=body.silent,
        )

    @app.post("/api/photos", dependencies=[Depends(require_auth)])
    async def send_photo(request: Request, body: PhotoRequest) -> dict[str, Any]:
        return await _manager(request).send_photo(
            account_id=body.account,
            to=body.to,
            image=body.image,
            caption=body.caption,
            silent=body.silent,
        )

    @app.post("/api/tts", dependencies=[Depends(require_auth)])
    async def send_tts(request: Request, body: TtsRequest) -> dict[str, Any]:
        return await _manager(request).send_tts(
            account_id=body.account,
            to=body.to,
            text=body.message,
            voice=body.voice,
            rate=body.rate,
            caption=body.caption,
        )

    @app.post("/api/calls", dependencies=[Depends(require_auth)])
    async def start_call(request: Request, body: CallRequest) -> dict[str, Any]:
        info = await _manager(request).start_call(
            account_id=body.account,
            to=body.to,
            text=body.message,
            voice=body.voice,
            rate=body.rate,
            ring_timeout=body.ring_timeout,
        )
        if body.wait and info.task is not None:
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(info.task), body.wait_seconds)
        return info.public()

    @app.get("/api/calls", dependencies=[Depends(require_auth)])
    async def list_calls(request: Request) -> dict[str, Any]:
        return {"calls": _manager(request).public_calls()}

    @app.get("/api/calls/{call_id}", dependencies=[Depends(require_auth)])
    async def get_call(request: Request, call_id: str) -> dict[str, Any]:
        for call in _manager(request).public_calls():
            if call["id"] == call_id:
                return call
        raise HTTPException(status_code=404, detail="call not found")

    @app.post("/api/calls/{call_id}/hangup", dependencies=[Depends(require_auth)])
    async def hangup_call(request: Request, call_id: str) -> dict[str, int]:
        return {"hungup": await _manager(request).hangup(call_id=call_id)}

    @app.post("/api/hangup", dependencies=[Depends(require_auth)])
    async def hangup(request: Request, body: HangupRequest) -> dict[str, int]:
        return {
            "hungup": await _manager(request).hangup(
                call_id=body.call_id,
                account_id=body.account,
            )
        }

    @app.post("/api/rings", dependencies=[Depends(require_auth)])
    async def start_ring(request: Request, body: RingRequest) -> dict[str, Any]:
        ring = await _manager(request).start_ring(
            account_id=body.account,
            to=body.to,
            text=body.message,
            voice=body.voice,
            rate=body.rate,
            attempts=body.attempts,
            interval=body.interval,
            ring_timeout=body.ring_timeout,
            send_message=body.send_message,
        )
        return ring.public()

    @app.get("/api/rings", dependencies=[Depends(require_auth)])
    async def list_rings(request: Request) -> dict[str, Any]:
        return {"rings": _manager(request).public_rings()}

    @app.post("/api/rings/{ring_id}/cancel", dependencies=[Depends(require_auth)])
    async def cancel_ring(request: Request, ring_id: str) -> dict[str, bool]:
        await _manager(request).cancel_ring(ring_id)
        return {"ok": True}

    @app.get("/api/logs", dependencies=[Depends(require_auth)])
    async def logs(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        kind: str | None = Query(default=None, max_length=32),
    ) -> dict[str, Any]:
        events: EventLog = request.app.state.events
        return {"events": events.snapshot(limit=limit, kind=kind)}

    @app.get("/api/voices", dependencies=[Depends(require_auth)])
    async def voices(request: Request) -> dict[str, Any]:
        cfg: AppSettings = request.app.state.settings
        names = WYOMING_VOICES if cfg.tts_engine == "wyoming" else VOICES
        return {"voices": list(names)}

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=API_PORT,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
