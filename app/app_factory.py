from contextlib import asynccontextmanager
import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.db.session import SessionLocal, init_db
from app.services.registration import process_player_registration_lifecycle
from app.web.routes import router as web_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _should_init_db():
        try:
            init_db()
        except Exception:
            logger.exception("Super Admin startup failed while initializing the database")
            raise

    async def _registration_housekeeping_loop() -> None:
        while True:
            try:
                with SessionLocal() as db:
                    process_player_registration_lifecycle(db)
            except Exception:
                pass
            await asyncio.sleep(24 * 60 * 60)

    app.state.registration_housekeeping_task = asyncio.create_task(_registration_housekeeping_loop())
    yield
    task = getattr(app.state, "registration_housekeeping_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


def create_app(app_mode: str = "combined") -> FastAPI:
    title = settings.app_name
    if app_mode == "super_admin":
        title = f"{settings.app_name} - Super Admin"
    elif app_mode == "team_admin":
        title = f"{settings.app_name} - Team Admin"

    app = FastAPI(title=title, lifespan=lifespan)
    app.state.app_mode = app_mode

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def _favicon_response():
        favicon_path = static_dir / "images" / "logo.jpg"
        if not favicon_path.is_file():
            return Response(status_code=204)
        return FileResponse(favicon_path, media_type="image/jpeg")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico():
        return _favicon_response()

    @app.get("/favicon.png", include_in_schema=False)
    async def favicon_png():
        return _favicon_response()

    @app.middleware("http")
    async def strip_vercel_api_prefix(request: Request, call_next):
        path = request.scope.get("path", "")
        if path.startswith("/api/index"):
            request.scope["path"] = path.removeprefix("/api/index") or "/"
        elif path.startswith("/api/super_admin"):
            request.scope["path"] = path.removeprefix("/api/super_admin") or "/"
        return await call_next(request)

    @app.middleware("http")
    async def app_mode_guard(request: Request, call_next):
        path = request.url.path
        if app_mode == "super_admin":
            blocked = path.startswith("/team-admin") or path.startswith(
                "/register/team-admin"
            )
            if blocked:
                return RedirectResponse("/login", status_code=303)

        if app_mode == "team_admin" and (
            path.startswith("/super-admin") or path.startswith("/register/super-admin")
        ):
            return RedirectResponse("/login", status_code=303)

        return await call_next(request)

    app.include_router(web_router)
    return app


def _should_init_db() -> bool:
    if _is_vercel_deployment():
        return True
    raw_value = os.getenv("RUN_DB_INIT")
    if raw_value is not None:
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}
    return True


def _is_vercel_deployment() -> bool:
    return bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV") or os.getenv("VERCEL_URL"))
