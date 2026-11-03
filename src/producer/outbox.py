"""Durable, at-least-once document dispatch; consumers handle duplicate delivery."""

import asyncio
from datetime import timedelta
import json
import logging

logger = logging.getLogger(__name__)


async def enqueue_document_job(tx, document_id, bot_id, user_id, upload_id=None):
    payload = {"document_id": document_id, "bot_id": bot_id, "user_id": user_id}
    if upload_id:
        payload["upload_id"] = upload_id
    await tx.execute_raw(
        'INSERT INTO "DocumentJobOutbox" ("id", "payload") VALUES ($1, $2::jsonb) ON CONFLICT ("id") DO NOTHING',
        upload_id or document_id, json.dumps(payload),
    )


async def dispatch_one(db, publish):
    async with db.tx(timeout=timedelta(seconds=45)) as tx:
        rows = await tx.query_raw('''SELECT "id", "payload", "attempts" FROM "DocumentJobOutbox"
            WHERE "nextAttemptAt" <= (clock_timestamp() AT TIME ZONE 'UTC')
            ORDER BY "nextAttemptAt" LIMIT 1 FOR UPDATE SKIP LOCKED''')
        if not rows:
            return False
        row = rows[0]
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        try:
            async with asyncio.timeout(25):
                await publish(payload["document_id"], payload["bot_id"], payload["user_id"], payload.get("upload_id"))
        except Exception as error:
            delay = min(60, 2 ** min(row["attempts"] + 1, 6))
            await tx.execute_raw('''UPDATE "DocumentJobOutbox" SET "attempts"="attempts"+1,
                "nextAttemptAt"=(clock_timestamp() AT TIME ZONE 'UTC') + $2::integer * INTERVAL '1 second'
                WHERE "id"=$1''', row["id"], delay)
            logger.error("Document dispatch delayed error_type=%s attempt=%s", type(error).__name__, row["attempts"] + 1)
        else:
            await tx.execute_raw('DELETE FROM "DocumentJobOutbox" WHERE "id"=$1', row["id"])
        return True


async def run_dispatcher(db, publish):
    while True:
        try:
            handled = await dispatch_one(db, publish)
        except Exception as error:
            logger.error("Document dispatcher unavailable error_type=%s", type(error).__name__)
            handled = False
        if not handled:
            await asyncio.sleep(2)
