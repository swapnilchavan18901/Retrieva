from fastapi import APIRouter, Depends, HTTPException
from src.producer.producer import publish_document_job
from src.db import prisma
from src.deps.auth import get_current_user
from src.schemas.documentation import CreateUploadUrlRequest, CompleteUploadRequest
from src.utils.s3 import generate_presigned_upload_url
import uuid
import os

router = APIRouter(tags=["documents"])


async def owned_bot(bot_id, user_id):
    bot = await prisma.bot.find_unique(where={"id": bot_id})
    if not bot or bot.userId != user_id:
        raise HTTPException(status_code=404, detail="Assistant not found")
    return bot


@router.get("/bots/{bot_id}/documents")
async def list_documents(bot_id: str, user=Depends(get_current_user)):
    await owned_bot(bot_id, user.id)
    documents = await prisma.document.find_many(
        where={"botId": bot_id}, order={"createdAt": "desc"},
        include={"uploads": {"order_by": {"createdAt": "desc"}, "take": 1}},
    )
    return [{"id": doc.id, "fileName": doc.fileName, "status": doc.status,
             "hasIndexedVersion": bool(doc.activeRevision) or doc.status == "ready",
             "latestUploadStatus": doc.uploads[0].status if doc.uploads else None,
             "createdAt": doc.createdAt} for doc in documents]


@router.post("/bots/{bot_id}/documents/upload-url")
async def create_upload_url(bot_id: str, payload: CreateUploadUrlRequest, user=Depends(get_current_user)):
    await owned_bot(bot_id, user.id)
    if os.path.splitext(payload.fileName)[1].lower() != ".pdf" or payload.fileType != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    document_id = payload.documentId or str(uuid.uuid4())
    if payload.documentId:
        document = await prisma.document.find_unique(where={"id": document_id})
        if not document or document.botId != bot_id:
            raise HTTPException(status_code=404, detail="Document not found")
    upload_id = str(uuid.uuid4())
    s3_key = f"documents/{bot_id}/{document_id}/{upload_id}.pdf"
    upload_url = generate_presigned_upload_url(s3_key)
    data = {"fileName": payload.fileName, "fileType": payload.fileType,
            "fileSize": payload.fileSize, "storageUrl": s3_key}
    async with prisma.tx() as tx:
        if not payload.documentId:
            await tx.document.create(data={"id": document_id, "botId": bot_id, **data, "status": "queued"})
        await tx.documentupload.create(data={"id": upload_id, "documentId": document_id, **data})
    return {"documentId": document_id, "uploadId": upload_id, "uploadUrl": upload_url, "s3Key": s3_key}


@router.post("/documents/{document_id}/complete")
async def complete_document_upload(document_id: str, payload: CompleteUploadRequest | None = None, user=Depends(get_current_user)):
    document = await prisma.document.find_unique(where={"id": document_id}, include={"bot": True})
    if not document or document.bot.userId != user.id:
        raise HTTPException(status_code=404, detail="Document not found")
    if payload and payload.uploadId:
        upload = await prisma.documentupload.find_unique(where={"id": payload.uploadId})
    else:
        # Compatibility for clients that don't yet send uploadId.
        upload = await prisma.documentupload.find_first(where={"documentId": document_id}, order={"createdAt": "desc"})
    if upload:
        if upload.documentId != document_id:
            raise HTTPException(status_code=404, detail="Upload not found")
        if upload.status in ("queued", "processing", "ready", "superseded"):
            return {"success": True, "documentId": document_id, "status": upload.status}
        claimed = await prisma.documentupload.update_many(where={"id": upload.id, "status": {"in": ["pending", "failed"]}}, data={"status": "queued"})
        if not claimed:
            raise HTTPException(status_code=409, detail="Upload already submitted")
        try:
            await publish_document_job(document_id, document.botId, user.id, upload.id)
        except Exception:
            await prisma.documentupload.update_many(where={"id": upload.id, "status": "queued"}, data={"status": "pending"})
            raise HTTPException(status_code=503, detail="Could not queue document. Please retry.") from None
    elif payload and payload.uploadId:
        raise HTTPException(status_code=404, detail="Upload not found")
    else:
        # Jobs/uploads created before the incremental-indexing migration.
        if document.status == "ready":
            return {"success": True, "documentId": document_id, "status": "ready"}
        await publish_document_job(document_id, document.botId, user.id)
    return {"success": True, "documentId": document_id, "status": "queued"}
