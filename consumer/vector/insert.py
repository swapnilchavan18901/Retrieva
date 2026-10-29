# ingestion/chunk_embed.py

import uuid
import asyncio
from qdrant_client.models import PointStruct
from consumer.vector.qdrantdb import qdrant_client
from consumer.vector.vector import ensure_collection
from shared.embeddings import collection_for_bot, embed_texts

# Text splitter configuration
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200


def split_text_recursive(text: str, chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP, separators: list[str] = None) -> list[str]:
    """
    Simple recursive text splitter - splits text into chunks with overlap.
    Alternative to LangChain's RecursiveCharacterTextSplitter.
    """
    if separators is None:
        separators = ["\n\n", "\n", ". ", " ", ""]
    
    chunks = []
    
    def _split(text: str, separators: list[str]) -> list[str]:
        if not text:
            return []
        
        # Try each separator
        for i, separator in enumerate(separators):
            if separator == "":
                # Last separator - split by character
                return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size - chunk_overlap)]
            
            if separator in text:
                splits = text.split(separator)
                result = []
                current_chunk = ""
                
                for split in splits:
                    # Re-add separator except for empty splits
                    split_with_sep = split + separator if split else ""
                    
                    if len(current_chunk) + len(split_with_sep) <= chunk_size:
                        current_chunk += split_with_sep
                    else:
                        if current_chunk:
                            result.append(current_chunk)
                        # If single split is larger than chunk_size, recursively split it
                        if len(split_with_sep) > chunk_size:
                            result.extend(_split(split_with_sep, separators[i + 1:]))
                            current_chunk = ""
                        else:
                            current_chunk = split_with_sep
                
                if current_chunk:
                    result.append(current_chunk)
                
                return result
        
        return [text]
    
    chunks = _split(text, separators)
    
    # Add overlap between chunks
    if chunk_overlap > 0 and len(chunks) > 1:
        overlapped_chunks = []
        for i, chunk in enumerate(chunks):
            if i == 0:
                overlapped_chunks.append(chunk)
            else:
                # Take overlap from previous chunk
                overlap = chunks[i - 1][-chunk_overlap:] if len(chunks[i - 1]) >= chunk_overlap else chunks[i - 1]
                overlapped_chunks.append(overlap + chunk)
        return overlapped_chunks
    
    return chunks


async def embed_texts_async(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed document chunks through OpenRouter with the model shared with queries."""
    return await embed_texts(texts, batch_size=batch_size)

async def _embed_and_store_async(
    docs,
    bot_id: str,
    doc_id: str,
    file_name: str,
    user_id: str
):
    """Async helper for embedding and storing"""
    # Get collection name based on bot_id
    collection_name = collection_for_bot(bot_id)
    
    # Don't print the entire document list - it's too long
    print(f"📚 Processing {len(docs)} pages from '{file_name}' for bot '{bot_id}'")
    print(f"   Target collection: '{collection_name}'")
    
    # Check if documents have any content
    pages_with_content = sum(1 for doc in docs if doc.page_content and doc.page_content.strip())
    print(f"   Pages with content: {pages_with_content}/{len(docs)}")
    
    if pages_with_content == 0:
        error_msg = (
            f"PDF '{file_name}' has no extractable text content. "
            f"This is likely a scanned/image-based PDF. "
            f"Please use OCR or upload a text-based PDF."
        )
        print(f"❌ {error_msg}")
        raise ValueError(error_msg)
    
    # Filter out empty pages before splitting
    docs_with_content = [doc for doc in docs if doc.page_content and doc.page_content.strip()]
    print(f"   Splitting {len(docs_with_content)} non-empty pages into chunks...")
    
    # Split each document into chunks
    all_chunks = []
    for doc in docs_with_content:
        text = doc.page_content
        text_chunks = split_text_recursive(text, CHUNK_SIZE, CHUNK_OVERLAP)
        
        # Keep track of metadata for each chunk
        for chunk_text in text_chunks:
            all_chunks.append({
                "text": chunk_text,
                "page": doc.metadata.get("page"),
                "metadata": doc.metadata
            })
    
    print(f"   ✅ Created {len(all_chunks)} chunks")
    
    if not all_chunks:
        raise ValueError(f"No chunks created from document '{file_name}'. Document may be empty.")
    
    print(f"   🔄 Generating embeddings for {len(all_chunks)} chunks...")
    texts = [chunk["text"] for chunk in all_chunks]
    vectors = await embed_texts_async(texts)
    print(f"   ✅ Embeddings generated")

    print(f"   📦 Preparing {len(all_chunks)} points for Qdrant...")
    points = []
    for i, (chunk, vector) in enumerate(zip(all_chunks, vectors)):
        points.append(
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "doc_id": doc_id,
                    "file_name": file_name,
                    "user_id": user_id,
                    "page": chunk.get("page"),
                    "chunk_index": i,
                    "text": chunk["text"]
                }
            )
        )

    # Batch upsert for better performance and to avoid timeouts
    print(f"   💾 Upserting {len(points)} points to '{collection_name}' in batches...")
    batch_size = 100
    total_batches = (len(points) + batch_size - 1) // batch_size
    # think of way we can do this in parallel 
    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        print(f"      Batch {batch_num}/{total_batches}: Upserting {len(batch)} points...")
        # Use asyncio.to_thread for synchronous Qdrant client
        await asyncio.to_thread(
            qdrant_client.upsert,
            collection_name=collection_name,
            points=batch
        )
    
    print(f"✅ Successfully stored {len(points)} chunks in collection '{collection_name}'")

async def embed_and_store(
    docs,
    bot_id: str,
    doc_id: str,
    file_name: str,
    user_id: str
):
    """Async function for embedding and storing"""
    # Ensure collection exists for this bot_id before storing
    await ensure_collection(bot_id)
    
    # Directly call the async function
    await _embed_and_store_async(
        docs,
        bot_id,
        doc_id,
        file_name,
        user_id
    )
