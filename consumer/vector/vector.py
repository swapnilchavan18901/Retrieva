import asyncio
from qdrant_client.models import Distance, VectorParams
from consumer.vector.qdrantdb import qdrant_client
from shared.embeddings import VECTOR_SIZE, collection_for_bot

def _ensure_collection_sync(bot_id: str):
    """Synchronous helper for ensuring collection exists based on bot_id"""
    collection_name = collection_for_bot(bot_id)
    if qdrant_client.collection_exists(collection_name):
        vectors = qdrant_client.get_collection(collection_name).config.params.vectors
        if not isinstance(vectors, VectorParams) or vectors.size != VECTOR_SIZE or vectors.distance != Distance.COSINE:
            raise ValueError(f"Collection '{collection_name}' does not match text-embedding-3-small's vector schema")
        print(f"✅ Collection '{collection_name}' already exists")
        return
    
    print(f"📦 Creating new collection '{collection_name}'...")
    qdrant_client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(
            size=VECTOR_SIZE,
            distance=Distance.COSINE,
        ),
    )
    print(f"✅ Collection '{collection_name}' created successfully")

async def ensure_collection(bot_id: str):
    """Async wrapper for ensuring collection exists based on bot_id"""
    await asyncio.to_thread(_ensure_collection_sync, bot_id)
