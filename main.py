import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from src.routes.auth import router as auth_router
from src.db import prisma
from src.env import env
from src.routes.bots import router as bots_router
from src.routes.api_key import router as api_key_router
from src.sockets.ws_chats import socket_app
from src.routes.documenation import router as documentation_router
from src.routes.testing import router as testing_router
from src.routes.chats import router as chats_router
from src.http_limits import HttpRateLimiter
from src.middleware import ApiProtectionMiddleware
from src.producer.outbox import run_dispatcher
from src.producer.producer import publish_document_job

logger = logging.getLogger(__name__)
http_limiter = HttpRateLimiter(prisma)


async def cleanup_http_limits():
    while True:
        try:
            await http_limiter.cleanup()
        except Exception as error:
            logger.error("HTTP counter cleanup failed error_type=%s", type(error).__name__)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app):
    await prisma.connect()
    tasks = [
        asyncio.create_task(cleanup_http_limits()),
        asyncio.create_task(run_dispatcher(prisma, publish_document_job)),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await prisma.disconnect()


app = FastAPI(
    title="Retrieva", lifespan=lifespan,
    docs_url=None if env.ENVIRONMENT == "production" else "/docs",
    redoc_url=None if env.ENVIRONMENT == "production" else "/redoc",
    openapi_url=None if env.ENVIRONMENT == "production" else "/openapi.json",
)

app.add_middleware(
    ApiProtectionMiddleware, limiter=http_limiter,
    requests_per_minute=env.HTTP_REQUESTS_PER_MINUTE,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=env.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After", "X-Request-ID"],
)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error("Unhandled request error request_id=%s error_type=%s", request_id, type(error).__name__)
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    origin = request.headers.get("origin")
    if origin and origin in env.CORS_ORIGINS:
        headers.update({"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Expose-Headers": "X-Request-ID", "Vary": "Origin"})
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected error occurred. Please try again.", "requestId": request_id},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    # Pydantic's default input field can contain passwords or API keys.
    return JSONResponse(status_code=422, content={
        "detail": [{"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in error.errors()],
        "requestId": getattr(request.state, "request_id", "unknown"),
    })


@app.get("/")
async def status():
    return {"status": "ok", "service": "Retrieva"}


@app.get("/health")
async def health():
    database_connected = prisma.is_connected()
    if database_connected:
        try:
            async with asyncio.timeout(2):
                await prisma.query_raw("SELECT 1 AS healthy")
        except Exception:
            database_connected = False
    return JSONResponse(
        status_code=200 if database_connected else 503,
        content={
            "status": "ok" if database_connected else "unhealthy",
            "database": "connected" if database_connected else "disconnected",
        },
    )


app.include_router(auth_router)
app.include_router(bots_router)
app.include_router(api_key_router)
app.include_router(documentation_router)
app.include_router(testing_router)
app.include_router(chats_router)
app.mount("/", socket_app)
