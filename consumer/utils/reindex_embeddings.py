"""Re-embed a different model's chunks into the bot's OpenAI embedding collection.

Original text-embedding-3-small collections do not need migration.
Run from FileMind/: python -m consumer.utils.reindex_embeddings
    --bot-id BOT_UUID --source-collection OLD_COLLECTION
Pause document uploads while migrating. Re-running safely upserts the same IDs.
"""

import argparse
import asyncio

from qdrant_client.models import PointStruct

from shared.embeddings import collection_for_bot, embed_texts


async def reindex_bot(bot_id: str, source_collection: str, page_size: int = 32) -> int:
    if page_size < 1:
        raise ValueError("Page size must be positive")
    target = collection_for_bot(bot_id)
    if source_collection == target:
        raise ValueError("Source and target are the same; original OpenAI vectors do not need migration")
    # Keep --help usable before API keys and database settings are configured.
    from consumer.vector.qdrantdb import qdrant_client
    from consumer.vector.vector import ensure_collection

    if not await asyncio.to_thread(qdrant_client.collection_exists, source_collection):
        raise ValueError(f"Source collection '{source_collection}' does not exist")

    await ensure_collection(bot_id)
    offset = None
    migrated = 0
    while True:
        records, next_offset = await asyncio.to_thread(
            qdrant_client.scroll,
            collection_name=source_collection,
            offset=offset,
            limit=page_size,
            with_payload=True,
            with_vectors=False,
        )
        texts = []
        for record in records:
            text = (record.payload or {}).get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Point '{record.id}' has no text to re-embed")
            texts.append(text)

        vectors = await embed_texts(texts)
        points = [
            PointStruct(id=record.id, vector=vector, payload=record.payload)
            for record, vector in zip(records, vectors)
        ]
        if points:
            await asyncio.to_thread(
                qdrant_client.upsert,
                collection_name=target,
                points=points,
                wait=True,
            )
        migrated += len(points)
        print(f"{bot_id}: re-embedded {migrated} chunks into {target}")
        if next_offset is None:
            break
        offset = next_offset

    print(f"Completed {bot_id}; legacy collection preserved.")
    return migrated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-id", required=True, help="Bot whose collection should receive the new vectors")
    parser.add_argument("--source-collection", required=True, help="Existing Gemini, BGE-M3, or other source collection")
    args = parser.parse_args()

    asyncio.run(reindex_bot(args.bot_id, args.source_collection))


if __name__ == "__main__":
    main()
