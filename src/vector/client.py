from qdrant_client import QdrantClient
from src.env import env

qdrant_client = QdrantClient(
    url=env.QDRANT_DB_CLUSTER_URL,
    api_key=env.QDRANT_DB_API_KEY,
    timeout=120,  # 2 minutes timeout for large batch operations
)

