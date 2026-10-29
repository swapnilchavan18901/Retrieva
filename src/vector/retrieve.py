from src.vector.client import qdrant_client
from shared.embeddings import collection_for_bot, embed_texts
import asyncio


async def embed_text(text: str) -> list[float]:
    return (await embed_texts([text]))[0]

async def retrieve_relevant_chunks(
    collection_name: str,
    question: str,
    top_k: int = 5
):
    # 1. Embed the question
    query_vector = await embed_text(question)
    
    # 2. Search Qdrant
    results = await asyncio.to_thread(
        qdrant_client.query_points,
        collection_name=collection_for_bot(collection_name),
        query=query_vector,
        limit=top_k,
    )

    # 3. Extract only relevant data for AI
    relevant_data = []
    for point in results.points:
        relevant_data.append({
            "text": point.payload.get("text"),
            "score": point.score,
            "file_name": point.payload.get("file_name"),
            "page": point.payload.get("page")
        })
    
    return relevant_data
