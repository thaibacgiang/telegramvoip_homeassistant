from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import unicodedata
import wave
from pathlib import Path

import edge_tts

from .const import (
    DEFAULT_TTS_ENGINE,
    DEFAULT_TTS_RATE,
    DEFAULT_TTS_VOICE,
    DEFAULT_WYOMING_HOST,
    DEFAULT_WYOMING_PORT,
    DEFAULT_WYOMING_VOICE,
)

_LOGGER = logging.getLogger(__name__)
_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
_MAX_TEXT = 2000
_EDGE_ATTEMPTS = 5
_WYOMING_ATTEMPTS = 3
_WYOMING_CONNECT_TIMEOUT = 10
_WYOMING_TIMEOUT = 90
_CACHE_VERSION = "v4"
_WYOMING_VOICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_EDGE_VOICE_RE = re.compile(r"^[A-Za-z]{2,3}-[A-Za-z]{2,4}-[A-Za-z0-9]+Neural$")


class TtsError(RuntimeError):
    pass


def normalize_rate(rate: str | None, default: str = DEFAULT_TTS_RATE) -> str:
    value = (rate or "").strip()
    return value if _RATE_RE.match(value) else default


def normalize_voice(voice: str | None, default: str = DEFAULT_TTS_VOICE) -> str:
    value = (voice or "").strip()
    if not re.match(r"^[A-Za-z]{2,3}-[A-Za-z]{2,4}-[A-Za-z0-9]+Neural$", value):
        return default
    return value


def normalize_wyoming_voice(voice: str | None, default: str = DEFAULT_WYOMING_VOICE) -> str:
    value = (voice or "").strip()
    # Giọng Edge (vi-VN-...Neural) không dùng được với Wyoming: quay về giọng mặc định.
    if _EDGE_VOICE_RE.match(value) or not _WYOMING_VOICE_RE.match(value):
        return default
    return value


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFC", text)
    value = value.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def espeak_language(voice: str) -> str:
    return voice.split("-", 1)[0].lower() or "vi"


class TtsService:
    def __init__(
        self,
        cache_dir: Path,
        ffmpeg: str | None = None,
        default_voice: str | None = None,
        default_rate: str | None = None,
        engine: str | None = None,
        wyoming_host: str | None = None,
        wyoming_port: int | None = None,
        wyoming_voice: str | None = None,
        fallback_edge: bool = True,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_voice = normalize_voice(default_voice)
        self.default_rate = normalize_rate(default_rate)
        self.engine = (engine or DEFAULT_TTS_ENGINE).strip().lower()
        if self.engine not in {"wyoming", "edge"}:
            self.engine = DEFAULT_TTS_ENGINE
        self.wyoming_host = (wyoming_host or DEFAULT_WYOMING_HOST).strip()
        self.wyoming_port = int(wyoming_port or DEFAULT_WYOMING_PORT)
        self.wyoming_voice = normalize_wyoming_voice(wyoming_voice)
        self.fallback_edge = fallback_edge
        self.ffmpeg = ffmpeg or os.environ.get("TGVOIP_FFMPEG") or shutil.which("ffmpeg")
        self.ffprobe = os.environ.get("TGVOIP_FFPROBE") or shutil.which("ffprobe")
        self._locks: dict[str, asyncio.Lock] = {}
        if self.engine == "wyoming":
            _LOGGER.info(
                "TTS engine=wyoming server=%s:%s voice=%s edge_fallback=%s",
                self.wyoming_host,
                self.wyoming_port,
                self.wyoming_voice,
                self.fallback_edge,
            )
        else:
            _LOGGER.info(
                "TTS engine=edge voice=%s rate=%s",
                self.default_voice,
                self.default_rate,
            )

    def resolve_voice(self, voice: str | None) -> str:
        if self.engine == "wyoming":
            return normalize_wyoming_voice(voice, self.wyoming_voice)
        return normalize_voice(voice, self.default_voice)

    def resolve_rate(self, rate: str | None) -> str:
        return normalize_rate(rate, self.default_rate)

    def cache_key(self, text: str, voice: str, rate: str) -> str:
        value = f"{_CACHE_VERSION}|{self.engine}|{voice}|{rate}|{text}".encode()
        return hashlib.sha256(value).hexdigest()[:24]

    def _require_ffmpeg(self) -> str:
        if not self.ffmpeg:
            raise TtsError("ffmpeg not found; install ffmpeg in the add-on image")
        return self.ffmpeg

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        rate: str | None = None,
    ) -> Path:
        clean = normalize_text(text)
        if not clean:
            raise TtsError("text is empty")
        if len(clean) > _MAX_TEXT:
            raise TtsError(f"text is longer than {_MAX_TEXT} characters")
        voice = self.resolve_voice(voice)
        rate = self.resolve_rate(rate)
        digest = self.cache_key(clean, voice, rate)
        target = self.cache_dir / f"{digest}.ogg"
        if target.is_file() and target.stat().st_size > 0:
            return target

        lock = self._locks.setdefault(digest, asyncio.Lock())
        async with lock:
            if target.is_file() and target.stat().st_size > 0:
                return target
            self._require_ffmpeg()
            with tempfile.TemporaryDirectory(prefix="tts-", dir=self.cache_dir) as tmp:
                tmp_dir = Path(tmp)
                fallback = False
                if self.engine == "wyoming":
                    try:
                        source = await self._wyoming_tts(clean, voice, tmp_dir)
                    except TtsError as err:
                        if not self.fallback_edge:
                            raise
                        _LOGGER.warning(
                            "Wyoming TTS failed (%s); falling back to Edge TTS", err
                        )
                        try:
                            source = await self._edge_tts(
                                clean, self.default_voice, rate, tmp_dir
                            )
                        except TtsError as edge_err:
                            raise TtsError(
                                f"Wyoming TTS failed ({err}); Edge TTS failed ({edge_err})"
                            ) from edge_err
                        fallback = True
                else:
                    try:
                        source = await self._edge_tts(clean, voice, rate, tmp_dir)
                    except TtsError as err:
                        if espeak_language(voice) == "vi":
                            _LOGGER.warning(
                                "Edge TTS failed after %s attempts for Vietnamese voice %s; "
                                "not using espeak-ng to avoid non-standard pronunciation: %s",
                                _EDGE_ATTEMPTS,
                                voice,
                                err,
                            )
                            raise TtsError(
                                "Edge TTS tạm thời không trả về audio cho giọng tiếng Việt; "
                                "vui lòng thử lại."
                            ) from err
                        _LOGGER.warning(
                            "Edge TTS failed after %s attempts (%s); falling back to espeak-ng",
                            _EDGE_ATTEMPTS,
                            err,
                        )
                        source = await self._espeak(clean, voice, tmp_dir)
                        fallback = True
                if fallback:
                    output = self.cache_dir / f"{digest}.fallback.ogg"
                else:
                    output = target
                await self._to_ogg(source, output)
            self._cleanup()
            return output

    async def _wyoming_tts(self, text: str, voice: str, tmp_dir: Path) -> Path:
        last_error: Exception | None = None
        for attempt in range(_WYOMING_ATTEMPTS):
            try:
                pcm, rate, width, channels = await asyncio.wait_for(
                    self._wyoming_once(text, voice),
                    timeout=_WYOMING_TIMEOUT,
                )
                if len(pcm) < 1024:
                    raise TtsError("Wyoming returned empty audio")
                source = tmp_dir / f"speech-{attempt}.wav"
                with wave.open(str(source), "wb") as wav:
                    wav.setnchannels(channels)
                    wav.setsampwidth(width)
                    wav.setframerate(rate)
                    wav.writeframes(bytes(pcm))
                if attempt:
                    _LOGGER.info("Wyoming TTS succeeded on attempt %s", attempt + 1)
                return source
            except (TtsError, OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as err:
                last_error = err
                _LOGGER.warning(
                    "Wyoming TTS attempt %s/%s failed (%s:%s): %s",
                    attempt + 1,
                    _WYOMING_ATTEMPTS,
                    self.wyoming_host,
                    self.wyoming_port,
                    err or type(err).__name__,
                )
                if attempt < _WYOMING_ATTEMPTS - 1:
                    await asyncio.sleep(1.0)
        raise TtsError(
            f"{self.wyoming_host}:{self.wyoming_port}: {last_error or 'Wyoming TTS failed'}"
        ) from last_error

    async def _wyoming_once(self, text: str, voice: str) -> tuple[bytearray, int, int, int]:
        """Gửi một yêu cầu `synthesize` theo giao thức Wyoming và gom audio PCM."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.wyoming_host, self.wyoming_port),
            timeout=_WYOMING_CONNECT_TIMEOUT,
        )
        pcm = bytearray()
        rate = width = channels = 0
        try:
            request = {"type": "synthesize", "data": {"text": text, "voice": {"name": voice}}}
            writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    raise TtsError("Wyoming server closed the connection early")
                header = json.loads(line)
                data = dict(header.get("data") or {})
                data_length = int(header.get("data_length") or 0)
                if data_length:
                    data.update(json.loads(await reader.readexactly(data_length)))
                payload_length = int(header.get("payload_length") or 0)
                payload = await reader.readexactly(payload_length) if payload_length else b""
                kind = header.get("type")
                if kind == "audio-start":
                    rate = int(data.get("rate") or 0)
                    width = int(data.get("width") or 0)
                    channels = int(data.get("channels") or 0)
                elif kind == "audio-chunk":
                    if not rate:
                        rate = int(data.get("rate") or 0)
                        width = int(data.get("width") or 0)
                        channels = int(data.get("channels") or 0)
                    pcm.extend(payload)
                elif kind == "audio-stop":
                    break
                elif kind == "error":
                    raise TtsError(str(data.get("text") or data.get("code") or "Wyoming error"))
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if not (rate and width and channels):
            raise TtsError("Wyoming returned no audio format")
        return pcm, rate, width, channels

    async def _edge_tts(self, text: str, voice: str, rate: str, tmp_dir: Path) -> Path:
        last_error: Exception | None = None
        for attempt in range(_EDGE_ATTEMPTS):
            source = tmp_dir / f"speech-{attempt}.mp3"
            try:
                communicate = edge_tts.Communicate(text, voice, rate=rate)
                await communicate.save(str(source))
                if source.is_file() and source.stat().st_size > 1024:
                    if attempt:
                        _LOGGER.info("Edge TTS succeeded on attempt %s", attempt + 1)
                    return source
                last_error = TtsError("Edge TTS returned empty audio")
            except Exception as err:
                last_error = err
            source.unlink(missing_ok=True)
            if attempt < _EDGE_ATTEMPTS - 1:
                await asyncio.sleep(min(0.8 * (attempt + 1), 2.0))
        raise TtsError(str(last_error or "Edge TTS failed")) from last_error

    async def _espeak(self, text: str, voice: str, tmp_dir: Path) -> Path:
        binary = shutil.which("espeak-ng")
        if not binary:
            raise TtsError("Edge TTS failed and espeak-ng is not installed")
        source = tmp_dir / "speech.wav"
        await self._exec(
            binary,
            "-v",
            espeak_language(voice),
            "-s",
            "155",
            "-w",
            str(source),
            text,
            timeout=60,
        )
        if not source.is_file() or source.stat().st_size == 0:
            raise TtsError("espeak-ng produced no audio")
        return source

    async def _to_ogg(self, source: Path, target: Path) -> None:
        tmp_target = target.with_suffix(".part.ogg")
        await self._exec(
            self._require_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            # Thêm khoảng lặng cuối câu để Telegram VoIP không cắt cụt âm tiết/từ cuối.
            "-af",
            "apad=pad_dur=0.8",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "libopus",
            "-b:a",
            "64k",
            "-application",
            "audio",
            "-f",
            "ogg",
            str(tmp_target),
            timeout=90,
        )
        if not tmp_target.is_file() or tmp_target.stat().st_size == 0:
            raise TtsError("ffmpeg could not create an OGG/Opus file")
        os.replace(tmp_target, target)

    async def _exec(self, *args: str, timeout: int = 60) -> None:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as err:
            process.kill()
            await process.wait()
            raise TtsError(f"command timed out: {args[0]}") from err
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[-400:]
            raise TtsError(f"{Path(args[0]).name} failed: {detail}")

    async def duration(self, path: Path) -> float:
        if self.ffprobe:
            process = await asyncio.create_subprocess_exec(
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
                return max(1.0, float(stdout.decode().strip()))
            except (TimeoutError, ValueError):
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        return 30.0

    def _cleanup(self) -> None:
        cutoff = time.time() - 7 * 24 * 3600
        try:
            for item in self.cache_dir.glob("*.ogg"):
                if item.stat().st_mtime < cutoff:
                    item.unlink(missing_ok=True)
        except OSError:
            pass

    def stats(self) -> dict[str, int]:
        try:
            files = list(self.cache_dir.glob("*.ogg"))
        except OSError:
            files = []
        return {
            "files": len(files),
            "bytes": sum(item.stat().st_size for item in files if item.is_file()),
        }
