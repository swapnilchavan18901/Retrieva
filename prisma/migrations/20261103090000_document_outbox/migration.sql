-- Persist queue intent atomically with upload status; delivery can be retried.
CREATE TABLE "DocumentJobOutbox" (
    "id" TEXT PRIMARY KEY,
    "payload" JSONB NOT NULL,
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "nextAttemptAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX "DocumentJobOutbox_nextAttemptAt_idx" ON "DocumentJobOutbox"("nextAttemptAt");
