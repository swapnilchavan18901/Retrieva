"""Stage incremental document revisions without re-embedding unchanged text."""

import asyncio
from collections import defaultdict
from hashlib import sha256
import uuid

from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct, PointIdsList, SetPayload, SetPayloadOperation
from consumer.vector.qdrantdb import qdrant_client
from consumer.vector.vector import ensure_collection
from shared.chunking import CHUNKER_VERSION, structure_chunks
from shared.embeddings import EMBEDDING_MODEL, collection_for_bot, embed_texts


def document_filter(doc_id: str, user_id: str) -> Filter:
    return Filter(must=[
        FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
    ])


async def document_points(bot_id: str, doc_id: str, user_id: str):
    records, offset = [], None
    while True:
        batch, offset = await asyncio.to_thread(
            qdrant_client.scroll, collection_name=collection_for_bot(bot_id),
            scroll_filter=document_filter(doc_id, user_id), limit=100,
            offset=offset, with_payload=True, with_vectors=True,
        )
        records.extend(batch)
        if offset is None:
            return records


async def embed_texts_async(texts: list[str], batch_size: int = 32):
    return await embed_texts(texts, batch_size=batch_size)


async def update_payloads(bot_id: str, updates):
    # Reused vectors stay in place. Batch only their version/position metadata.
    operations = [SetPayloadOperation(set_payload=SetPayload(points=[point_id], payload=payload))
                  for point_id, payload in updates]
    for start in range(0, len(operations), 100):
        await asyncio.to_thread(qdrant_client.batch_update_points, collection_name=collection_for_bot(bot_id),
                                update_operations=operations[start:start + 100], wait=True)


async def embed_and_store(docs, bot_id: str, doc_id: str, file_name: str, user_id: str,
                          revision: str | None = None, legacy_active: bool = False):
    """Stage a revision. Caller publishes its DB pointer before cleanup_revision()."""
    revision = revision or doc_id
    chunks = []
    for page in docs:
        for chunk in structure_chunks(page.page_content or ""):
            chunks.append({**chunk, "page": page.metadata.get("page")})
    if not chunks:
        raise ValueError("PDF has no extractable text content; use OCR for scanned PDFs")

    await ensure_collection(bot_id)
    existing = await document_points(bot_id, doc_id, user_id)
    by_text = defaultdict(list)
    cached_vectors = {}
    for point in existing:
        payload = point.payload or {}
        if payload.get("embedding_model", EMBEDDING_MODEL) != EMBEDDING_MODEL:
            continue
        text = payload.get("text")
        if isinstance(text, str) and isinstance(point.vector, list):
            by_text[text].append(point)
            cached_vectors[text] = point.vector
    for points in by_text.values():
        points.sort(key=lambda point: str(point.id))

    # Compare the exact embedding inputs. Metadata and chunk position aren't part of the hash.
    missing = list(dict.fromkeys(chunk["text"] for chunk in chunks if chunk["text"] not in cached_vectors))
    vectors = await embed_texts_async(missing) if missing else []
    if len(vectors) != len(missing):
        raise ValueError("Unexpected embedding count; old revision has been preserved")
    cached_vectors.update(zip(missing, vectors))

    occurrences = defaultdict(int)
    new_points, retained_updates = [], []
    retained_ids = set()
    for index, chunk in enumerate(chunks):
        text = chunk["text"]
        digest = sha256(text.encode("utf-8")).hexdigest()
        occurrence = occurrences[text]
        occurrences[text] += 1
        old = by_text[text][occurrence] if occurrence < len(by_text[text]) else None
        point_id = old.id if old else str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{EMBEDDING_MODEL}:{digest}:{occurrence}"))
        previous = dict(old.payload or {}) if old else {}
        revisions = list(previous.get("revisions", []))
        metadata = dict(previous.get("revision_metadata", {}))
        # Preserve legacy queries until the first versioned update is activated.
        if old and not revisions and legacy_active:
            revisions.append("legacy")
            metadata["legacy"] = {key: previous.get(key) for key in ("file_name", "page", "chunk_index", "section")}
        if revision not in revisions:
            revisions.append(revision)
        metadata[revision] = {"file_name": file_name, "page": chunk["page"], "chunk_index": index, "section": chunk["section"]}
        payload = {
            **previous, "doc_id": doc_id, "user_id": user_id, "text": text,
            "text_hash": digest, "embedding_model": EMBEDDING_MODEL,
            "chunker_version": CHUNKER_VERSION,
            "revisions": revisions, "revision_metadata": metadata,
        }
        if old:
            retained_ids.add(str(point_id))
            retained_updates.append((point_id, payload))
        else:
            new_points.append(PointStruct(id=point_id, vector=cached_vectors[text], payload=payload))

    # All embedding calls finish before any existing payload changes or deletions.
    for start in range(0, len(new_points), 100):
        await asyncio.to_thread(qdrant_client.upsert, collection_name=collection_for_bot(bot_id), points=new_points[start:start + 100], wait=True)
    await update_payloads(bot_id, retained_updates)
    stats = {"total": len(chunks), "embedded": len(missing), "reused": len(retained_ids),
             "removed": sum(str(point.id) not in retained_ids for point in existing)}
    print(f"Document {doc_id}: {stats['embedded']} new embeddings, {stats['reused']} reused chunks, {stats['removed']} obsolete chunks")
    return stats


async def cleanup_revision(bot_id: str, doc_id: str, user_id: str, revision: str):
    """Run after activation, under the same per-document writer lock."""
    points = await document_points(bot_id, doc_id, user_id)
    obsolete = [point.id for point in points if revision not in (point.payload or {}).get("revisions", [])]
    if obsolete:
        await asyncio.to_thread(qdrant_client.delete, collection_name=collection_for_bot(bot_id), points_selector=PointIdsList(points=obsolete), wait=True)
    updates = []
    for point in points:
        payload = point.payload or {}
        if revision in payload.get("revisions", []):
            current = payload.get("revision_metadata", {}).get(revision, {})
            updates.append((point.id, {"revisions": [revision], "revision_metadata": {revision: current}}))
    await update_payloads(bot_id, updates)
