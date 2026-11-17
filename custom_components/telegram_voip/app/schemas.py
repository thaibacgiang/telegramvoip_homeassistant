from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CredentialsUpdateRequest(BaseModel):
    api_id: int | None = Field(default=None, ge=1, le=2_147_483_647)
    api_hash: str | None = Field(default=None, min_length=16, max_length=128)
    api_token: str | None = Field(default=None, min_length=16, max_length=256)

class LoginStartRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=5, max_length=32)
    api_id: int | None = Field(default=None, ge=1)
    api_hash: str | None = Field(default=None, max_length=128)


class LoginVerifyRequest(BaseModel):
    code: str | None = Field(default=None, max_length=16)
    password: str | None = Field(default=None, max_length=256)


class MessageRequest(BaseModel):
    account: str | None = None
    to: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4096)
    parse_mode: Literal["", "plain", "html", "markdown"] | None = None
    silent: bool = False


class PhotoRequest(BaseModel):
    account: str | None = None
    to: str = Field(min_length=1, max_length=128)
    image: str = Field(min_length=1, max_length=12_000_000)
    caption: str | None = Field(default=None, max_length=1024)
    silent: bool = False

class TtsRequest(BaseModel):
    account: str | None = None
    to: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    voice: str | None = Field(default=None, max_length=64)
    rate: str | None = Field(default=None, max_length=12)
    caption: str | None = Field(default=None, max_length=1024)


class CallRequest(BaseModel):
    account: str | None = None
    to: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    voice: str | None = Field(default=None, max_length=64)
    rate: str | None = Field(default=None, max_length=12)
    ring_timeout: int | None = Field(default=None, ge=10, le=180)
    wait: bool = False
    wait_seconds: int = Field(default=60, ge=5, le=180)


class RingRequest(BaseModel):
    account: str | None = None
    to: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    voice: str | None = Field(default=None, max_length=64)
    rate: str | None = Field(default=None, max_length=12)
    attempts: int | None = Field(default=None, ge=1, le=10)
    interval: int | None = Field(default=None, ge=0, le=600)
    ring_timeout: int | None = Field(default=None, ge=10, le=180)
    send_message: bool = True


class HangupRequest(BaseModel):
    call_id: str | None = None
    account: str | None = None
