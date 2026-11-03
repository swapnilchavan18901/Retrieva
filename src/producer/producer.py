import json
import aio_pika
from src.producer.rabbit_mq import get_connection

QUEUE = "document_process_queue"

async def publish_document_job(document_id: str, bot_id: str, user_id: str, upload_id: str | None = None):
    connection = await get_connection()
    payload = {
        "document_id": document_id,
        "bot_id": bot_id,
        "user_id": user_id
    }
    if upload_id:
        payload["upload_id"] = upload_id

    message = aio_pika.Message(
            body=json.dumps(payload).encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
            message_id=upload_id or document_id,
        )

    try:
        channel = await connection.channel(publisher_confirms=True)
        await channel.declare_queue(QUEUE, durable=True)
        await channel.default_exchange.publish(message, routing_key=QUEUE, timeout=10)
    finally:
        await connection.close()
