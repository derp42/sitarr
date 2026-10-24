from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .config import Config, ProtectedUser
from .db import Store
from .engine import Engine
from .models import Guidance, Item, proposal
from .servers import Jellyfin, ServerError

LOG = logging.getLogger("sitarr")
ROOT = Path(__file__).parent


class JobRequest(BaseModel):
    kind: Literal["scan", "resolve", "sync", "apply"]
    key: str | None = None
    review_url: str | None = None


class OverrideRequest(BaseModel):
    age: int = Field(ge=0, le=21)
    reason: str = Field(min_length=3, max_length=2000)


def create_app(config: Config | None = None, start_worker=True):
    config = config or Config()
    store = Store(config.directory / "sitarr.db")
    engine = Engine(config, store)
    templates = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
    security = HTTPBasic()

    def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
        valid_user = secrets.compare_digest(credentials.username.encode(), config.username.encode())
        valid_password = secrets.compare_digest(credentials.password.encode(), config.password.encode())
        if not (valid_user and valid_password):
            raise HTTPException(
                401, "Invalid credentials", headers={"WWW-Authenticate": 'Basic realm="Sitarr"'}
            )

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            engine.start()
        yield
        engine.stop()

    app = FastAPI(
        title="Sitarr",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config, app.state.store, app.state.engine = config, store, engine
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        # Browser writes must originate from this instance. No cross-origin
        # cookie credentials, wildcard CORS, or secrets in query-string APIs.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return Response("Cross-origin writes are not allowed", status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return Response("Cross-site writes are not allowed", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(authenticate)])
    def home():
        return templates.get_template("index.html").render(version=__version__)

    @app.get("/api/status", dependencies=[Depends(authenticate)])
    def status():
        return {
            **store.stats(),
            "version": __version__,
            "auto_apply": config.settings.auto_apply,
            "servers": [
                {"id": s.id, "name": s.name, "kind": s.kind, "enabled": s.enabled}
                for s in config.settings.servers
            ],
            "protected_users": [u.model_dump() for u in config.settings.protected_users],
        }

    @app.get("/api/items", dependencies=[Depends(authenticate)])
    def items(q="", status="", limit: int = Query(100, ge=1, le=250), offset: int = Query(0, ge=0)):
        data = store.items(q, status, limit, offset)
        for row in data["items"]:
            row["proposal"] = proposal(
                Item(**row["snapshot"]), Guidance(**row["guidance"]) if row["guidance"] else None
            )
        return data

    @app.get("/api/jobs", dependencies=[Depends(authenticate)])
    def jobs():
        return store.jobs()

    @app.post("/api/jobs", dependencies=[Depends(authenticate)])
    def queue_job(job: JobRequest):
        if job.key and not store.get(job.key):
            raise HTTPException(404, "Title not found")
        return {"id": store.queue(job.kind, job.model_dump(exclude={"kind"}, exclude_none=True))}

    @app.post("/api/items/{key}/override", dependencies=[Depends(authenticate)])
    def override(key: str, override: OverrideRequest):
        row = store.get(key)
        if not row:
            raise HTTPException(404, "Title not found")
        item = Item(**json.loads(row["snapshot"]))
        guidance = Guidance(
            override.age,
            "Household override",
            "",
            "manual review",
            item.title,
            item.year,
            {"reason": override.reason},
        )
        store.guidance(key, guidance, override.reason)
        return {"status": "ready", "guidance": guidance.data()}

    @app.get("/api/changes", dependencies=[Depends(authenticate)])
    def changes():
        return store.ledger()

    @app.post("/api/changes/{change_id}/undo", dependencies=[Depends(authenticate)])
    def undo(change_id: int):
        return {"id": store.queue("undo", {"change_id": change_id})}

    @app.get("/api/export", dependencies=[Depends(authenticate)])
    def export():
        return Response(
            json.dumps({"version": __version__, "changes": store.ledger(1_000_000)}, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="sitarr-ledger.json"'},
        )

    @app.get("/api/settings", dependencies=[Depends(authenticate)])
    def settings():
        return config.public()

    @app.put("/api/settings", dependencies=[Depends(authenticate)])
    def update_settings(payload: dict):
        try:
            config.update_public(payload)
        except (ValidationError, ValueError):
            # Pydantic errors can echo secret input values; don't return them.
            raise HTTPException(422, "Check server URLs, IDs, age limits, and numeric settings") from None
        return config.public()

    @app.get("/api/servers/{server_id}/test", dependencies=[Depends(authenticate)])
    def test_server(server_id: str):
        server = engine.server(server_id)
        try:
            libraries = server.libraries()
            users = []
            if isinstance(server, Jellyfin):
                users = [
                    {
                        "id": u["Id"],
                        "name": u["Name"],
                        "admin": u["Policy"]["IsAdministrator"],
                        "max_rating": u["Policy"].get("MaxParentalRating"),
                    }
                    for u in server.users()
                ]
            return {"ok": True, "libraries": libraries, "users": users}
        except ServerError as exc:
            raise HTTPException(502, str(exc)) from None
        finally:
            server.close()

    @app.post("/api/protect", dependencies=[Depends(authenticate)])
    def protect(user: ProtectedUser):
        return {"id": store.queue("protect", user.model_dump())}

    return app


app = create_app()
