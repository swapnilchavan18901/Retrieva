"""Durable, atomic admission control shared by all API processes."""
import hashlib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DAILY_OUTPUT_TOKEN_LIMIT = 50_000  # Per bot, shared by ALL its API keys.
MAX_OUTPUT_TOKENS = 768
MAX_MESSAGE_CHARACTERS = 4_000
REPLY_TIMEOUT_SECONDS = 90
CONNECTION_LEASE_SECONDS = 90
GENERATION_LEASE_SECONDS = 150
INDIA = ZoneInfo("Asia/Kolkata")


@dataclass
class LimitDenied(Exception):
    code: str
    message: str
    retry_after: int = 1

    def payload(self):
        return {"code": self.code, "message": self.message, "retryAfter": self.retry_after}


@dataclass
class Admission:
    id: str
    max_tokens: int


def validate_message(data):
    if not isinstance(data, dict) or not isinstance(data.get("content"), str) or not data["content"].strip():
        raise LimitDenied("invalid_message", "Please enter a message.", 0)
    if len(data["content"]) > MAX_MESSAGE_CHARACTERS:
        raise LimitDenied("message_too_long", f"Please keep messages under {MAX_MESSAGE_CHARACTERS:,} characters.", 0)


def ip_hash(environ):
    # Do not trust client-controlled X-Forwarded-For. Configure trusted proxy IPs
    # in Uvicorn so REMOTE_ADDR / ASGI client contains the verified client IP.
    scope = environ.get("asgi.scope", {})
    address = (scope.get("client") or (environ.get("REMOTE_ADDR", "unknown"),))[0]
    return hashlib.sha256(str(address).encode()).hexdigest()


def windows(now, specifications):
    epoch = int(now.timestamp())
    for name, identity, capacity, seconds in specifications:
        reset = (epoch // seconds + 1) * seconds
        yield f"{name}:{identity}:{epoch // seconds}", capacity, datetime.fromtimestamp(reset, timezone.utc), reset - epoch


class ChatLimits:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        async with self.db.tx(timeout=timedelta(seconds=10), max_wait=timedelta(seconds=5)) as tx:
            await tx.query_raw("SELECT 1 AS locked FROM pg_advisory_xact_lock(438215709)")
            rows = await tx.query_raw('SELECT CURRENT_TIMESTAMP AS "now"')
            now = rows[0]["now"]
            if isinstance(now, str):
                now = datetime.fromisoformat(now.replace("Z", "+00:00"))
            now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
            await tx.execute_raw('DELETE FROM "ChatLimitLease" WHERE "expiresAt" <= CURRENT_TIMESTAMP')
            await tx.execute_raw('DELETE FROM "ChatLimitCounter" WHERE "expiresAt" <= CURRENT_TIMESTAMP')
            yield tx, now

    async def hit(self, tx, now, specifications):
        for key, capacity, reset, retry in windows(now, specifications):
            rows = await tx.query_raw('SELECT "value" FROM "ChatLimitCounter" WHERE "key"=$1', key)
            if rows and rows[0]["value"] >= capacity:
                return LimitDenied("rate_limited", "Too many requests. Please wait and try again.", retry)
            await tx.execute_raw('INSERT INTO "ChatLimitCounter" ("key","value","expiresAt") VALUES ($1,1,$2::timestamp) '
                                 'ON CONFLICT ("key") DO UPDATE SET "value"="ChatLimitCounter"."value"+1', key, reset)
        return None

    async def connection_attempt(self, address):
        async with self.transaction() as (tx, now):
            denied = await self.hit(tx, now, [("connect", address, 20, 60)])
        if denied:
            raise denied

    async def connect(self, sid, bot, key, address):
        denied = None
        async with self.transaction() as (tx, now):
            rows = await tx.query_raw('SELECT "ipHash","apiKeyId" FROM "ChatLimitLease" WHERE "kind"=\'connection\'')
            if len(rows) >= 500 or sum(row["ipHash"] == address for row in rows) >= 5 or sum(row["apiKeyId"] == key for row in rows) >= 100:
                denied = LimitDenied("connection_limit", "Too many open chat connections. Please try again shortly.", 30)
            else:
                await self.insert(tx, sid, "connection", bot, key, "", address, now + timedelta(seconds=CONNECTION_LEASE_SECONDS))
        if denied:
            raise denied

    async def renew(self, sid):
        # Expired leases cannot be revived: a missing lease disconnects the client.
        rows = await self.db.query_raw('UPDATE "ChatLimitLease" SET "expiresAt"=CURRENT_TIMESTAMP + INTERVAL \'90 seconds\' '
            'WHERE "id"=$1 AND "kind"=\'connection\' AND "expiresAt">CURRENT_TIMESTAMP RETURNING "id"', sid)
        return bool(rows)

    async def disconnect(self, sid):
        await self.db.execute_raw('DELETE FROM "ChatLimitLease" WHERE "id"=$1 AND "kind"=\'connection\'', sid)

    async def control(self, session):
        async with self.transaction() as (tx, now):
            active = await tx.query_raw('SELECT "id" FROM "ChatLimitLease" WHERE "kind"=\'generation\' AND "sessionId"=$1', session.get("sessionId", ""))
            denied = LimitDenied("reply_in_progress", "Please wait for the current reply.") if active else await self.hit(tx, now, [
                ("session_control_ip", session["ipHash"], 20, 60),
                ("session_control_key", session["apiKeyId"], 60, 60)])
        if denied:
            raise denied

    async def acquire(self, session):
        bot, key, chat, address = (session[k] for k in ("botId", "apiKeyId", "sessionId", "ipHash"))
        rows = await self.db.query_raw(
            'SELECT retrieva_admit_chat($1,$2,$3,$4,$5,$6::integer,$7::integer) AS result',
            str(uuid.uuid4()), bot, key, chat, address, MAX_OUTPUT_TOKENS, DAILY_OUTPUT_TOKEN_LIMIT)
        result = rows[0]["result"]
        if isinstance(result, str):
            import json
            result = json.loads(result)
        if result.get("code"):
            messages = {
                "unauthorized": "This chat is unavailable. Please reconnect.",
                "reply_in_progress": "Please wait for the current reply.",
                "busy": "The assistant is busy. Please try again shortly.",
                "rate_limited": "Too many requests. Please wait and try again.",
                "daily_token_limit": "This assistant has reached its daily allowance. Please try again tomorrow.",
            }
            raise LimitDenied(result["code"], messages[result["code"]], result["retry"])
        return Admission(result["id"], result["max_tokens"])

    async def insert(self, tx, identity, kind, bot, key, chat, address, expires, day=None, reserve=0):
        await tx.execute_raw('INSERT INTO "ChatLimitLease" ("id","kind","botId","apiKeyId","sessionId","ipHash","expiresAt","dayKey","reservedTokens") '
            'VALUES ($1,$2,$3,$4,$5,$6,$7::timestamp,$8,$9)', identity, kind, bot, key, chat, address, expires, day, reserve)

    async def finish(self, admission, output_tokens=None):
        async with self.transaction() as (tx, now):
            rows = await tx.query_raw('DELETE FROM "ChatLimitLease" WHERE "id"=$1 AND "kind"=\'generation\' RETURNING "dayKey","reservedTokens"', admission.id)
            if rows and isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens >= 0:
                row = rows[0]
                await tx.execute_raw('UPDATE "ChatLimitCounter" SET "value"=GREATEST(0,"value"+$2) WHERE "key"=$1',
                    row["dayKey"], output_tokens - row["reservedTokens"])
