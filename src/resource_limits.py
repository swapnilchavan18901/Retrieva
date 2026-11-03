"""Serialize quota checks and creation in the same database transaction."""

from fastapi import HTTPException


async def enforce_quota(tx, resource, identity, model, where, maximum):
    # A per-resource lock avoids both creation races and a global bottleneck.
    await tx.query_raw("SELECT 1 AS locked FROM pg_advisory_xact_lock(hashtextextended($1, 0))", f"quota:{resource}:{identity}")
    count = await getattr(tx, model).count(where=where)
    if count >= maximum:
        raise HTTPException(status_code=409, detail=f"{resource} limit reached ({maximum}).")
    return count
