"""Bound REST request work before handlers run; leave Socket.IO streaming intact."""

import asyncio
import json
import logging
from uuid import uuid4

from starlette.responses import JSONResponse

from src.http_limits import RatePolicy

logger = logging.getLogger(__name__)


class ApiProtectionMiddleware:
    def __init__(self, app, limiter, requests_per_minute=120, max_body_bytes=65_536):
        self.app = app
        self.limiter = limiter
        self.general_policy = RatePolicy("all", requests_per_minute, 60)
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                headers.update({
                    b"x-request-id": request_id.encode(),
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"strict-origin-when-cross-origin",
                    b"cache-control": b"no-store",
                })
                message = {**message, "headers": list(headers.items())}
            await send(message)

        async def reject(status, detail, headers=None):
            await JSONResponse(
                {"detail": detail, "requestId": request_id}, status_code=status, headers=headers,
            )(scope, receive, send_with_headers)

        async def admit(identity, policies):
            try:
                async with asyncio.timeout(3):
                    for policy in policies:
                        retry = await self.limiter.check(identity, policy)
                        if retry:
                            await reject(429, "Too many requests. Please try again later.", {"Retry-After": str(retry)})
                            return False
            except Exception as error:
                logger.error("Rate limiter unavailable request_id=%s error_type=%s", request_id, type(error).__name__)
                await reject(503, "Service temporarily unavailable. Please try again.", {"Retry-After": "5"})
                return False
            return True

        path = scope["path"].rstrip("/") or "/"
        if path == "/socket.io" or path.startswith("/socket.io/") or scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send_with_headers)

        # Liveness/readiness remain reachable during overload or DB outages.
        if path not in {"/", "/health"}:
            identity = str((scope.get("client") or ("unknown",))[0])
            policies = [self.general_policy]
            if scope["method"] == "POST":
                specific = {
                    "/auth/login": RatePolicy("login", 10, 60),
                    "/auth/register": RatePolicy("register", 5, 3600),
                    "/auth/demo": RatePolicy("demo", 5, 60),
                }.get(path)
                if specific:
                    policies.append(specific)
                if path.endswith("/documents/upload-url"):
                    policies.append(RatePolicy("upload", 15, 60))
            if not await admit(identity, policies):
                return

        # Buffer only bounded REST bodies. This also covers chunked requests that
        # omit Content-Length, before any handler can cause a side effect.
        headers = dict(scope.get("headers", []))
        if b"content-length" in headers:
            try:
                length = int(headers[b"content-length"])
                if length < 0:
                    raise ValueError
            except ValueError:
                return await reject(400, "Invalid Content-Length.")
            if length > self.max_body_bytes:
                return await reject(413, "Request body is too large.")
        chunks, size = [], 0
        try:
            async with asyncio.timeout(15):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > self.max_body_bytes:
                        return await reject(413, "Request body is too large.")
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "Request body timed out.")

        body = b"".join(chunks)
        if path == "/auth/login" and scope["method"] == "POST":
            try:
                data = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                data = None
            email = data.get("email") if isinstance(data, dict) else None
            if isinstance(email, str) and 0 < len(email) <= 254:
                # Shared across source IPs; only the digest is stored in the DB.
                if not await admit(email.strip().casefold(), [RatePolicy("login_account", 20, 900)]):
                    return
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send_with_headers)
