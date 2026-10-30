ALTER TABLE "Document" ADD COLUMN "activeRevision" TEXT;
ALTER TABLE "Document" ADD COLUMN "activeRevisionCreatedAt" TIMESTAMP(3);

CREATE TABLE "DocumentUpload" (
    "id" TEXT NOT NULL,
    "documentId" TEXT NOT NULL,
    "fileName" TEXT NOT NULL,
    "fileType" TEXT NOT NULL,
    "fileSize" BIGINT NOT NULL,
    "storageUrl" TEXT NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "DocumentUpload_pkey" PRIMARY KEY ("id")
);
CREATE INDEX "DocumentUpload_documentId_createdAt_idx" ON "DocumentUpload"("documentId", "createdAt");
ALTER TABLE "DocumentUpload" ADD CONSTRAINT "DocumentUpload_documentId_fkey"
    FOREIGN KEY ("documentId") REFERENCES "Document"("id") ON DELETE CASCADE ON UPDATE CASCADE;
