import json
import aio_pika
from src.producer.rabbit_mq import get_connection

QUEUE = "document_process_queue"

async def publish_document_job(document_id: str, bot_id: str, user_id: str, upload_id: str | None = None):
    connection = await get_connection()
    channel = await connection.channel()

    # Declare durable queue
    await channel.declare_queue(
        QUEUE,
        durable=True
    )

    payload = {
        "document_id": document_id,
        "bot_id": bot_id,
        "user_id": user_id
    }
    if upload_id:
        payload["upload_id"] = upload_id

    message = aio_pika.Message(
            body=json.dumps(payload).encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT
        )

    await channel.default_exchange.publish(
        message,
        routing_key=QUEUE
    )

    await connection.close()
    print("📨 Job published:", payload)
