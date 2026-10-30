import asyncio
from qdrant_client.models import FieldCondition, Filter, IsEmptyCondition, MatchValue, PayloadField
from src.vector.client import qdrant_client
from src.db import prisma
from shared.embeddings import collection_for_bot, embed_texts
from shared.vector_indexes import ensure_revision_indexes


async def embed_text(text: str) -> list[float]:
    return (await embed_texts([text]))[0]


def active_revision_filter(documents):
    conditions = []
    for doc in documents:
        identity = FieldCondition(key="doc_id", match=MatchValue(value=doc.id))
        if doc.activeRevision:
            conditions.append(Filter(must=[identity, FieldCondition(key="revisions", match=MatchValue(value=doc.activeRevision))]))
        else:
            # Legacy points remain searchable until this document's first versioned update.
            conditions.append(Filter(must=[identity, Filter(should=[
                IsEmptyCondition(is_empty=PayloadField(key="revisions")),
                FieldCondition(key="revisions", match=MatchValue(value="legacy")),
            ])]))
    return Filter(should=conditions)


async def retrieve_relevant_chunks(collection_name: str, question: str, top_k: int = 5):
    documents = await prisma.document.find_many(where={"botId": collection_name})
    if not documents:
        return []
    collection = collection_for_bot(collection_name)
    if not await asyncio.to_thread(qdrant_client.collection_exists, collection):
        return []
    await asyncio.to_thread(ensure_revision_indexes, qdrant_client, collection)
    query_vector = await embed_text(question)
    results = await asyncio.to_thread(qdrant_client.query_points,
        collection_name=collection, query=query_vector, limit=top_k,
        query_filter=active_revision_filter(documents))
    revisions = {doc.id: doc.activeRevision or "legacy" for doc in documents}
    relevant_data = []
    for point in results.points:
        payload = point.payload or {}
        revision = revisions.get(payload.get("doc_id"))
        metadata = payload.get("revision_metadata", {}).get(revision, payload)
        relevant_data.append({"text": payload.get("text"), "score": point.score,
                              "file_name": metadata.get("file_name"), "page": metadata.get("page"),
                              "section": metadata.get("section")})
    return relevant_data
