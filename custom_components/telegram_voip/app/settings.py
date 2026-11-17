from __future__ import annotations

import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .const import (
    DEFAULT_LOG_LIMIT,
    DEFAULT_MAX_CALL_SECONDS,
    DEFAULT_RING_ATTEMPTS,
    DEFAULT_RING_INTERVAL,
    DEFAULT_RING_TIMEOUT,
    DEFAULT_TTS_ENGINE,
    DEFAULT_TTS_FALLBACK_EDGE,
    DEFAULT_TTS_RATE,
    DEFAULT_TTS_VOICE,
    DEFAULT_WYOMING_HOST,
    DEFAULT_WYOMING_PORT,
    DEFAULT_WYOMING_VOICE,
)

_LOGGER = logging.getLogger(__name__)


def _as_int(value: Any, default: int, minimum: int = 1, maximum: int = 3600) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


@dataclass(slots=True)
class AppSettings:
    data_dir: Path
    api_token: str
    api_id: int | None = None
    api_hash: str = ""
    tts_voice: str = DEFAULT_TTS_VOICE
    tts_rate: str = DEFAULT_TTS_RATE
    ring_timeout: int = DEFAULT_RING_TIMEOUT
    ring_attempts: int = DEFAULT_RING_ATTEMPTS
    ring_interval: int = DEFAULT_RING_INTERVAL
    max_call_seconds: int = DEFAULT_MAX_CALL_SECONDS
    log_limit: int = DEFAULT_LOG_LIMIT
    tts_engine: str = DEFAULT_TTS_ENGINE
    wyoming_host: str = DEFAULT_WYOMING_HOST
    wyoming_port: int = DEFAULT_WYOMING_PORT
    wyoming_voice: str = DEFAULT_WYOMING_VOICE
    tts_fallback_edge: bool = DEFAULT_TTS_FALLBACK_EDGE
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def accounts_file(self) -> Path:
        return self.data_dir / "accounts.json"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def tts_cache_dir(self) -> Path:
        return self.cache_dir / "tts"

    @property
    def api_token_file(self) -> Path:
        return self.data_dir / "api_token"


_ENV_KEYS = {
    "TGVOIP_API_ID": "api_id",
    "TGVOIP_API_HASH": "api_hash",
    "TGVOIP_API_TOKEN": "api_token",
    "TGVOIP_TTS_VOICE": "tts_voice",
    "TGVOIP_TTS_RATE": "tts_rate",
    "TGVOIP_RING_TIMEOUT": "ring_timeout",
    "TGVOIP_RING_ATTEMPTS": "ring_attempts",
    "TGVOIP_RING_INTERVAL": "ring_interval",
    "TGVOIP_MAX_CALL_SECONDS": "max_call_seconds",
    "TGVOIP_LOG_LIMIT": "log_limit",
    "TGVOIP_TTS_ENGINE": "tts_engine",
    "TGVOIP_WYOMING_HOST": "wyoming_host",
    "TGVOIP_WYOMING_PORT": "wyoming_port",
    "TGVOIP_WYOMING_VOICE": "wyoming_voice",
    "TGVOIP_TTS_FALLBACK_EDGE": "tts_fallback_edge",
}
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def _read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        _LOGGER.warning("Cannot read %s: %s", path, err)
        return {}
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        _LOGGER.warning("%s is accessible by group/others; run chmod 600", path)
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not _ENV_KEY_RE.fullmatch(key):
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = re.sub(
                    r'\\(["\\n])',
                    lambda match: {'"': '"', "\\": "\\", "n": "\n"}[match.group(1)],
                    value,
                )
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values

def env_file_path(data_path: Path) -> Path:
    return Path(os.environ.get("TGVOIP_ENV_FILE", data_path / ".env"))

def _quote_env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]*", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'

def save_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
        key, separator, _ = candidate.partition("=")
        key = key.strip()
        if separator and key in updates:
            output.append(f"{key}={_quote_env_value(updates[key])}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={_quote_env_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    temp_path.chmod(0o600)
    os.replace(temp_path, path)
    path.chmod(0o600)

def _load_env_overrides(data_path: Path) -> dict[str, Any]:
    env_path = env_file_path(data_path)
    values = _read_env_file(env_path) if env_path.is_file() else {}
    overrides: dict[str, Any] = {}
    for env_key, setting_key in _ENV_KEYS.items():
        if env_key in values:
            overrides[setting_key] = values[env_key]
    for env_key, setting_key in _ENV_KEYS.items():
        if env_key in os.environ:
            overrides[setting_key] = os.environ[env_key]
    return overrides

def load_settings(data_dir: str | Path | None = None) -> AppSettings:
    data_path = Path(data_dir or os.environ.get("TGVOIP_DATA_DIR", "/data"))
    data_path.mkdir(parents=True, exist_ok=True)

    options: dict[str, Any] = {}
    options_file = data_path / "options.json"
    if options_file.is_file():
        try:
            options = json.loads(options_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("Cannot read options.json: %s", err)
    if not isinstance(options, dict):
        options = {}
    options.update(_load_env_overrides(data_path))

    token = _as_text(options.get("api_token"))
    token_file = data_path / "api_token"
    if token:
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
    elif token_file.is_file():
        token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        _LOGGER.warning("Generated API token. Read it from %s", token_file)

    api_id_raw = options.get("api_id")
    api_id: int | None
    try:
        api_id = int(api_id_raw) if api_id_raw not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        api_id = None

    settings = AppSettings(
        data_dir=data_path,
        api_token=token,
        api_id=api_id,
        api_hash=_as_text(options.get("api_hash")),
        tts_voice=_as_text(options.get("tts_voice"), DEFAULT_TTS_VOICE) or DEFAULT_TTS_VOICE,
        tts_rate=_as_text(options.get("tts_rate"), DEFAULT_TTS_RATE) or DEFAULT_TTS_RATE,
        ring_timeout=_as_int(options.get("ring_timeout"), DEFAULT_RING_TIMEOUT, 10, 180),
        ring_attempts=_as_int(options.get("ring_attempts"), DEFAULT_RING_ATTEMPTS, 1, 10),
        ring_interval=_as_int(options.get("ring_interval"), DEFAULT_RING_INTERVAL, 0, 600),
        max_call_seconds=_as_int(
            options.get("max_call_seconds"), DEFAULT_MAX_CALL_SECONDS, 10, 900
        ),
        log_limit=_as_int(options.get("log_limit"), DEFAULT_LOG_LIMIT, 50, 2000),
        tts_engine=(
            _as_text(options.get("tts_engine"), DEFAULT_TTS_ENGINE).lower()
            if _as_text(options.get("tts_engine"), DEFAULT_TTS_ENGINE).lower()
            in {"wyoming", "edge"}
            else DEFAULT_TTS_ENGINE
        ),
        wyoming_host=_as_text(options.get("wyoming_host"), DEFAULT_WYOMING_HOST)
        or DEFAULT_WYOMING_HOST,
        wyoming_port=_as_int(options.get("wyoming_port"), DEFAULT_WYOMING_PORT, 1, 65535),
        wyoming_voice=_as_text(options.get("wyoming_voice"), DEFAULT_WYOMING_VOICE)
        or DEFAULT_WYOMING_VOICE,
        tts_fallback_edge=_as_bool(
            options.get("tts_fallback_edge"), DEFAULT_TTS_FALLBACK_EDGE
        ),
        options=options,
    )

    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    settings.sessions_dir.chmod(0o700)
    settings.tts_cache_dir.mkdir(parents=True, exist_ok=True)
    return settings


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 5:
        return "***"
    return f"+{digits[:2]}***{digits[-3:]}"
