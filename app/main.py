from contextlib import asynccontextmanager
from pathlib import Path
import base64
import secrets

import pycountry
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings
from app.db import make_engine, session_factory
from app.errors import AppError
from app.models import AuditEvent, Campaign, Creation, WorkspaceBinding
from app.schemas import AddOffer, CreateCampaign, PinOffer, Revision, SyncCampaign
from app.service import CampaignService, campaign_view, is_dirty
from app.tracker import DemoKeitaro, KeitaroClient, as_list

STATIC = Path(__file__).parent / "static"


def create_app(settings=None, tracker=None):
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    sessions = session_factory(engine)
    owns_tracker = tracker is None

    @asynccontextmanager
    async def lifespan(app):
        if not inspect(engine).has_table("workspace_binding"):
            raise RuntimeError(
                "Database is not initialized. Run: uv run alembic upgrade head"
            )
        identity = (
            "live:" + settings.keitaro_base_url.rstrip("/")
            if settings.adrobot_mode == "live"
            else "demo:"
            + str(Path(make_url(settings.demo_database_url).database).resolve())
        )
        with sessions.begin() as db:
            binding = db.get(WorkspaceBinding, 1)
            if binding and binding.tracker_identity != identity:
                raise RuntimeError(
                    "This database belongs to another tracker or mode. Set a separate DATABASE_URL and run migrations."
                )
            if not binding:
                db.add(WorkspaceBinding(id=1, tracker_identity=identity))
        app.state.tracker = tracker or (
            DemoKeitaro(settings)
            if settings.adrobot_mode == "demo"
            else KeitaroClient(settings)
        )
        yield
        if owns_tracker:
            app.state.tracker.close()
        engine.dispose()

    app = FastAPI(title="Adrobot API", version="1.0.0", lifespan=lifespan)
    app.state.settings, app.state.sessions, app.state.engine = (
        settings,
        sessions,
        engine,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[s.strip() for s in settings.allowed_hosts.split(",")],
    )

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if settings.app_username and request.url.path != "/healthz":
            authorization = request.headers.get("authorization", "")
            valid = False
            if authorization.startswith("Basic "):
                try:
                    username, password = (
                        base64.b64decode(authorization[6:], validate=True)
                        .decode()
                        .split(":", 1)
                    )
                    valid = secrets.compare_digest(
                        username.encode(), settings.app_username.encode()
                    ) & secrets.compare_digest(
                        password.encode(),
                        settings.app_password.get_secret_value().encode(),
                    )
                except (ValueError, UnicodeDecodeError):
                    pass
            if not valid:
                return JSONResponse(
                    {"message": "Требуется авторизация."},
                    401,
                    headers={"WWW-Authenticate": 'Basic realm="Adrobot"'},
                )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin and origin.rstrip("/") != str(request.base_url).rstrip("/")
            ):
                return JSONResponse(
                    {"message": "Запрос с другого сайта отклонён."}, 403
                )
            if (
                request.headers.get("content-type", "").split(";")[0]
                != "application/json"
            ):
                return JSONResponse({"message": "Ожидается application/json."}, 415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        return response

    @app.exception_handler(AppError)
    async def app_error(_, exc):
        return JSONResponse(
            {"message": exc.message, "code": exc.code, **exc.details},
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc):
        errors = [
            {"field": ".".join(map(str, e["loc"][1:])), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            {
                "message": "Проверьте поля формы.",
                "code": "validation_error",
                "errors": errors,
            },
            422,
        )

    @app.exception_handler(IntegrityError)
    async def conflict(_, exc):
        return JSONResponse(
            {
                "message": "Объект уже изменён параллельным запросом. Обновите страницу.",
                "code": "database_conflict",
            },
            409,
        )

    def db_session(request: Request):
        with sessions() as db:
            try:
                # SQLite has no SELECT FOR UPDATE. Acquire its write lock before reading.
                if request.method != "GET" and engine.dialect.name == "sqlite":
                    db.execute(text("BEGIN IMMEDIATE"))
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def service(request: Request, db=Depends(db_session)):
        return CampaignService(db, request.app.state.tracker, settings)

    @app.get("/healthz")
    def health():
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        return {
            "mode": settings.adrobot_mode,
            "campaign_prefix": settings.campaign_prefix,
            "countries": [
                {"code": c.alpha_2, "name": c.name}
                for c in sorted(pycountry.countries, key=lambda c: c.name)
            ],
        }

    @app.get("/api/catalog")
    def catalog(svc=Depends(service)):
        result = {}
        for key in ("domains", "groups", "traffic_sources"):
            rows = as_list(
                svc.tracker.request(
                    "GET",
                    key,
                    params={"type": "campaigns"} if key == "groups" else None,
                )
            )
            result[key] = [
                {"id": int(r["id"]), "name": r.get("name", str(r["id"]))}
                for r in rows
                if r.get("state", "active") == "active"
            ]
        return result

    @app.get("/api/offers")
    def offers(q: str = Query(default="", max_length=200), svc=Depends(service)):
        rows = as_list(svc.tracker.request("GET", "offers"))
        query = q.strip().casefold()
        rows = [
            r
            for r in rows
            if r.get("state", "active") == "active"
            and (
                not query
                or query in str(r["id"])
                or query in r.get("name", "").casefold()
            )
        ]
        return {
            "items": [
                {"id": int(r["id"]), "name": r.get("name", str(r["id"]))}
                for r in rows[:50]
            ],
            "total": len(rows),
        }

    @app.get("/api/campaigns")
    def campaigns(svc=Depends(service)):
        rows = svc.db.scalars(
            select(Campaign).order_by(Campaign.synced_at.desc())
        ).all()
        return [
            {
                "id": c.id,
                "name": c.name,
                "state": c.state,
                "dirty_count": sum(is_dirty(s) for s in c.streams),
            }
            for c in rows
        ]

    @app.get("/api/remote-campaigns")
    def remote_campaigns(
        q: str = Query(default="", max_length=200), svc=Depends(service)
    ):
        rows = as_list(svc.tracker.request("GET", "campaigns"))
        query = q.casefold()
        rows = [
            r
            for r in rows
            if r.get("state", "active") != "deleted"
            and (
                not query
                or query in r.get("name", "").casefold()
                or query in str(r["id"])
            )
        ]
        return [
            {
                "id": int(r["id"]),
                "name": r["name"],
                "writable": r["name"].startswith(settings.campaign_prefix),
            }
            for r in rows[:100]
        ]

    @app.post("/api/campaigns", status_code=201)
    def create_campaign(data: CreateCampaign, svc=Depends(service)):
        return svc.create(data)

    @app.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: int, svc=Depends(service)):
        return campaign_view(svc.campaign(campaign_id), settings.campaign_prefix)

    @app.post("/api/campaigns/{campaign_id}/sync")
    def sync_campaign(campaign_id: int, data: SyncCampaign, svc=Depends(service)):
        return svc.sync(campaign_id, data.discard_drafts)

    @app.get("/api/campaigns/{campaign_id}/events")
    def events(campaign_id: int, svc=Depends(service)):
        rows = svc.db.scalars(
            select(AuditEvent)
            .where(AuditEvent.campaign_id == campaign_id)
            .order_by(AuditEvent.id.desc())
            .limit(30)
        )
        return [
            {
                "action": r.action,
                "stream_id": r.stream_id,
                "details": r.details,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    @app.get("/api/operations/{key}")
    def operation(key: str, svc=Depends(service)):
        row = svc.db.get(Creation, key)
        if row is None:
            raise AppError("Операция не найдена.", 404, "not_found")
        return {
            "id": row.key,
            "status": row.status,
            "campaign_id": row.campaign_id,
            "message": row.message,
        }

    @app.post("/api/streams/{stream_id}/offers")
    def add_offer(stream_id: int, data: AddOffer, svc=Depends(service)):
        return svc.mutate(stream_id, data.revision, "add", data.offer_id)

    @app.post("/api/streams/{stream_id}/offers/{offer_id}/pin")
    def pin_offer(stream_id: int, offer_id: int, data: PinOffer, svc=Depends(service)):
        return svc.mutate(
            stream_id, data.revision, "pin", offer_id, data.pinned, data.share
        )

    @app.post("/api/streams/{stream_id}/offers/{offer_id}/{action}")
    def change_offer(
        stream_id: int, offer_id: int, action: str, data: Revision, svc=Depends(service)
    ):
        if action not in {"remove", "restore"}:
            raise AppError("Неизвестное действие.", 404)
        return svc.mutate(stream_id, data.revision, action, offer_id)

    @app.post("/api/streams/{stream_id}/{action}")
    def change_stream(
        stream_id: int, action: str, data: Revision, svc=Depends(service)
    ):
        if action == "publish":
            return svc.publish(stream_id, data.revision)
        if action not in {"cancel", "rebalance"}:
            raise AppError("Неизвестное действие.", 404)
        return svc.mutate(stream_id, data.revision, action)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return app


app = create_app()
