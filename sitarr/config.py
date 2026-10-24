from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class Server(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    kind: Literal["jellyfin", "plex"] = "jellyfin"
    name: str = "Jellyfin"
    url: str
    token: str = ""
    enabled: bool = True
    libraries: list[str] = []

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError("Use an HTTP(S) server URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Server URLs cannot include query strings or fragments")
        return value.rstrip("/")


class ProtectedUser(BaseModel):
    server_id: str
    user_id: str
    name: str
    max_age: int = Field(default=10, ge=0, le=21)
    block_unreviewed: bool = False


class Settings(BaseModel):
    servers: list[Server] = []
    protected_users: list[ProtectedUser] = []
    auto_apply: bool = False
    sync_hours: int = Field(default=6, ge=1, le=168)
    batch_size: int = Field(default=100, ge=1, le=2000)
    request_delay: float = Field(default=2.0, ge=1, le=60)
    cache_days: int = Field(default=30, ge=1, le=365)
    csm_enabled: bool = True
    mdblist_token: str = ""


class Config:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory or os.environ.get("SITARR_DATA", "data"))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "settings.json"
        self.lock = RLock()
        self.settings = (
            Settings.model_validate_json(self.path.read_text()) if self.path.exists() else Settings()
        )
        self.password_path = self.directory / "admin-password"
        if not self.password_path.exists():
            self.password_path.write_text(os.environ.get("SITARR_PASSWORD") or secrets.token_urlsafe(24))
            self.password_path.chmod(0o600)
        self.password = os.environ.get("SITARR_PASSWORD") or self.password_path.read_text().strip()
        self.username = os.environ.get("SITARR_USERNAME", "admin")

    def save(self, settings: Settings):
        with self.lock:
            ids = [s.id for s in settings.servers]
            if len(ids) != len(set(ids)):
                raise ValueError("Each server needs a unique ID")
            if any(u.server_id not in ids for u in settings.protected_users):
                raise ValueError("Protected users must belong to a configured server")
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(settings.model_dump_json(indent=2))
            temporary.chmod(0o600)
            temporary.replace(self.path)
            self.settings = settings

    def public(self) -> dict:
        data = self.settings.model_dump()
        for server in data["servers"]:
            server["token"] = "********" if server["token"] else ""
        data["mdblist_token"] = "********" if data["mdblist_token"] else ""
        return data

    def update_public(self, payload: dict):
        old = {s.id: s for s in self.settings.servers}
        for server in payload.get("servers", []):
            if server.get("token") == "********":
                server["token"] = old.get(server["id"], Server(id="new", url="http://localhost")).token
        if payload.get("mdblist_token") == "********":
            payload["mdblist_token"] = self.settings.mdblist_token
        self.save(Settings.model_validate(payload))

    def export(self) -> str:
        return json.dumps(self.public(), indent=2)
