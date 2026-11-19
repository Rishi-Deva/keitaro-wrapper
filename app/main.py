import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.keitaro.client import KeitaroClient
from app.keitaro.errors import (
    KeitaroAuthError,
    KeitaroError,
    KeitaroNotFound,
    KeitaroUnavailable,
    KeitaroValidationError,
)
from app.services.errors import ServiceError

STATIC = Path(__file__).parent / "static"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER, CSRF_VALUE = "X-Requested-With", "keitaro-wrapper"
log = logging.getLogger(__name__)


def _host_only(value: str) -> str:
    """'127.0.0.1:8000' -> '127.0.0.1', '[::1]:8000' / '[::1]' -> '::1', 'Localhost' -> 'localhost'."""
    v = value.strip().lower()
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else ""
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def create_app(settings: Settings | None = None, client: KeitaroClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or get_settings()
        app.state.settings = s
        engine = make_engine(s.database_url)
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        own_client = client is None
        app.state.keitaro = client or KeitaroClient(
            s.keitaro_api_base,
            s.keitaro_api_key.get_secret_value(),
            timeout=s.keitaro_timeout,
            connect_timeout=s.keitaro_connect_timeout,
        )
        try:
            yield
        finally:
            if own_client:
                await app.state.keitaro.aclose()
            engine.dispose()

    app = FastAPI(title="Keitaro wrapper", lifespan=lifespan)
    app.include_router(router)

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        s: Settings = request.app.state.settings
        # 1) DNS rebinding: only our own host names
        host = _host_only(request.headers.get("host") or "")
        if host not in s.allowed_hosts_list:
            return JSONResponse({"error": "bad_host", "detail": "Host not allowed", "fields": None}, status_code=400)
        # 2) CSRF: state-changing API calls need a custom header -> cross-site forms/simple requests can't send it,
        #    and cross-origin fetch with it needs a CORS preflight that we never allow.
        if request.method not in SAFE_METHODS and request.url.path.startswith("/api/"):
            if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return JSONResponse(
                    {"error": "csrf", "detail": f"Missing header {CSRF_HEADER}: {CSRF_VALUE}", "fields": None},
                    status_code=403,
                )
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, e: ServiceError) -> JSONResponse:
        return JSONResponse({"error": e.code, "detail": e.message, "fields": e.details}, status_code=e.status)

    @app.exception_handler(KeitaroError)
    async def _keitaro_error(_: Request, e: KeitaroError) -> JSONResponse:
        # e.message has method + path only, never host or key
        if isinstance(e, KeitaroValidationError):
            code, status = "keitaro_validation", 422
        elif isinstance(e, KeitaroNotFound):
            code, status = "not_found", 404
        elif isinstance(e, KeitaroAuthError):
            code, status = "keitaro_auth", 502
        elif isinstance(e, KeitaroUnavailable):
            code, status = "keitaro_unavailable", 504 if "timeout" in e.message else 502
        else:
            code, status = "keitaro_error", 502
        fields = e.details if isinstance(e, KeitaroValidationError) else None
        return JSONResponse({"error": code, "detail": e.message, "fields": fields}, status_code=status)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    if STATIC.exists():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


app = create_app()
