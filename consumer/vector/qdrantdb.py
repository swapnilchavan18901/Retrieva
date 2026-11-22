from qdrant_client import QdrantClient
from consumer.consumer_env import env

qdrant_client = QdrantClient(
    url=env.QDRANT_DB_CLUSTER_URL,
    api_key=env.QDRANT_DB_API_KEY,
    timeout=120,  # 2 minutes timeout for large batch operations
)

def collection_exists(bot_id: str) -> bool:
    """Check if a collection exists in Qdrant"""
    try:
        qdrant_client.get_collection(bot_id)
        return True
    except Exception:
        return False
