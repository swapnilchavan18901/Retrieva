import json
import asyncio
import aio_pika
import os
import logging
from pypdf.errors import PdfReadError
from datetime import timedelta
from shared.benchmark import benchmark_run
from botocore.exceptions import ClientError

from consumer.aws.file_loader import load_pdf_from_s3
from consumer.vector.insert import embed_and_store, cleanup_revision
from consumer.consumer_db import prisma
from consumer.consumer_env import env

RABBIT_MQ_URL = env.RABBIT_MQ_URL
QUEUE = "document_process_queue"
DEAD_LETTER_QUEUE = "document_process_dead_letter_queue"
MAX_JOB_RETRIES = 3
logger = logging.getLogger(__name__)


async def lock_document(tx, document_id):
    # Transaction-scoped PostgreSQL lock also serializes multiple worker processes.
    await tx.query_raw("SELECT 1 AS locked FROM pg_advisory_xact_lock(hashtextextended($1, 0))", document_id)


async def process_document(payload: dict):
    with benchmark_run("document_index", document_id=payload.get("document_id"),
                       bot_id=payload.get("bot_id"), upload_id=payload.get("upload_id")):
        await _process_document(payload)


async def _process_document(payload: dict):
    document_id, bot_id, user_id = (payload.get(key) for key in ("document_id", "bot_id", "user_id"))
    upload_id = payload.get("upload_id")
    if not all(isinstance(value, str) and value for value in (document_id, bot_id, user_id)):
        raise ValueError("Invalid document job")
    revision = upload_id or document_id
    activated = False
    verified = False
    try:
        # Keep the lock through staging and activation, so concurrent/retried updates cannot race.
        async with prisma.tx(timeout=timedelta(minutes=15), max_wait=timedelta(seconds=30)) as tx:
            await lock_document(tx, document_id)
            document = await tx.document.find_unique(where={"id": document_id}, include={"bot": True})
            if not document or document.botId != bot_id or document.bot.userId != user_id:
                raise ValueError("Document not found or job ownership mismatch")
            upload = await tx.documentupload.find_unique(where={"id": upload_id}) if upload_id else None
            if upload_id and (not upload or upload.documentId != document_id):
                raise ValueError("Upload not found")
            verified = True
            if upload and upload.status == "superseded":
                return
            if upload and document.activeRevisionCreatedAt and upload.createdAt < document.activeRevisionCreatedAt:
                await tx.documentupload.update(where={"id": upload_id}, data={"status": "superseded"})
                return
            if document.activeRevision == revision:
                activated = True  # Duplicate delivery: skip embeddings, retry any unfinished cleanup.
            elif not upload_id and document.activeRevision:
                return  # An old pre-migration job must never replace a newer revision.
            else:
                source = upload or document
                if upload:
                    await tx.documentupload.update(where={"id": upload_id}, data={"status": "processing"})
                docs, temp_path = await load_pdf_from_s3(source.storageUrl)
                try:
                    await embed_and_store(docs, bot_id, document_id, source.fileName, user_id,
                                          revision=revision, legacy_active=not document.activeRevision)
                finally:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                # A single DB pointer change publishes the complete revision to retrieval.
                await tx.document.update(where={"id": document_id}, data={
                    "activeRevision": revision, "activeRevisionCreatedAt": source.createdAt,
                    "fileName": source.fileName, "fileType": source.fileType,
                    "fileSize": source.fileSize, "storageUrl": source.storageUrl, "status": "ready",
                })
                if upload:
                    await tx.documentupload.update(where={"id": upload_id}, data={"status": "ready"})
        activated = True
        # Cleanup after commit; never remove the active old revision on an embedding failure.
        async with prisma.tx(timeout=timedelta(minutes=5), max_wait=timedelta(seconds=30)) as tx:
            await lock_document(tx, document_id)
            current = await tx.document.find_unique(where={"id": document_id})
            if current and current.activeRevision == revision:
                await cleanup_revision(bot_id, document_id, user_id, revision)
        print(f"✅ Indexed document {document_id}")
    except Exception:
        if verified and not activated:
            if upload_id:
                await prisma.documentupload.update_many(where={"id": upload_id, "status": {"not": "ready"}}, data={"status": "failed"})
            await prisma.document.update_many(where={"id": document_id, "activeRevision": None}, data={"status": "failed"})
        raise


async def publish_retry(message, queue, attempt):
    connection = await aio_pika.connect_robust(RABBIT_MQ_URL, timeout=10)
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        await channel.declare_queue(queue, durable=True)
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=message.body,
                headers={**(message.headers or {}), "x-retrieva-retry": attempt},
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                content_type="application/json",
                message_id=message.message_id,
            ),
            routing_key=queue, timeout=10,
        )


async def on_message(message: aio_pika.IncomingMessage):
    try:
        payload = json.loads(message.body)
        if not isinstance(payload, dict):
            raise ValueError("Invalid document job")
        await process_document(payload)
    except Exception as error:
        attempt = (message.headers or {}).get("x-retrieva-retry", 0)
        if type(attempt) is not int or not 0 <= attempt <= MAX_JOB_RETRIES:
            attempt = MAX_JOB_RETRIES
        missing_object = isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}
        permanent = isinstance(error, (ValueError, PdfReadError)) or missing_object
        exhausted = permanent or attempt >= MAX_JOB_RETRIES
        queue = DEAD_LETTER_QUEUE if exhausted else QUEUE
        logger.warning("Document job failed error_type=%s attempt=%s dead_letter=%s", type(error).__name__, attempt, exhausted)
        try:
            if not exhausted:
                await asyncio.sleep(2 ** attempt)
            # Confirm the replacement before ACK; worker processing is idempotent.
            await publish_retry(message, queue, attempt + 1)
        except Exception as publish_error:
            logger.error("Job recovery publish failed error_type=%s", type(publish_error).__name__)
            await asyncio.sleep(2)
            await message.reject(requeue=True)
            return
        await message.ack()
    else:
        await message.ack()


async def start_consumer():
    # Connect to Prisma before starting consumer
    print("🔌 Connecting to Prisma...")
    await prisma.connect()
    print("✅ Prisma connected")

    try:
        connection = await aio_pika.connect_robust(RABBIT_MQ_URL, timeout=10)

        async with connection:
            channel = await connection.channel()

            # Process ONE message at a time (important)
            await channel.set_qos(prefetch_count=1)

            queue = await channel.declare_queue(
                QUEUE,
                durable=True
            )

            await queue.consume(on_message)

            print("👂 Async consumer started. Waiting for messages...")
            await asyncio.Future()  # run forever
    finally:
        # Disconnect Prisma when shutting down
        print("🔌 Disconnecting Prisma...")
        await prisma.disconnect()
        print("✅ Prisma disconnected")


if __name__ == "__main__":
    asyncio.run(start_consumer())
