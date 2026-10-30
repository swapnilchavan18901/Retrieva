import asyncio
from qdrant_client.models import FieldCondition, Filter, IsEmptyCondition, MatchValue, PayloadField
from src.vector.client import qdrant_client
from src.db import prisma
from shared.embeddings import collection_for_bot, embed_texts
from shared.vector_indexes import ensure_revision_indexes
from shared.benchmark import benchmark_stage


_ready_collections = set()


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
    collection = collection_for_bot(collection_name)
    async def prepare_collection():
        exists = await asyncio.to_thread(qdrant_client.collection_exists, collection)
        if exists and collection not in _ready_collections:
            await asyncio.to_thread(ensure_revision_indexes, qdrant_client, collection)
            _ready_collections.add(collection)
        return exists
    with benchmark_stage("retrieval_prepare"):
        documents, exists = await asyncio.gather(
            prisma.document.find_many(where={"botId": collection_name}), prepare_collection())
    if not documents or not exists:
        return []
    with benchmark_stage("query_embedding"):
        query_vector = await embed_text(question)
    with benchmark_stage("qdrant_search"):
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
