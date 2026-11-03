"""Cross-process HTTP throttling using the existing PostgreSQL counter table."""

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class RatePolicy:
    name: str
    capacity: int
    seconds: int


class HttpRateLimiter:
    def __init__(self, db):
        self.db = db

    async def check(self, identity: str, policy: RatePolicy) -> int:
        """Return zero when admitted, otherwise seconds until the window resets.

        The conditional upsert admits at most capacity requests, including races
        across workers. Database time avoids clock skew between API processes.
        Stable keys reuse expired rows instead of creating a row every minute.
        """
        digest = hashlib.sha256(identity.encode()).hexdigest()
        key = f"http:{policy.name}:{digest}"
        rows = await self.db.query_raw(
            '''INSERT INTO "ChatLimitCounter" ("key", "value", "expiresAt")
            VALUES ($1, 1, (clock_timestamp() AT TIME ZONE 'UTC') + $3::integer * INTERVAL '1 second')
            ON CONFLICT ("key") DO UPDATE SET
                "value" = CASE WHEN "ChatLimitCounter"."expiresAt" <= (clock_timestamp() AT TIME ZONE 'UTC')
                    THEN 1 ELSE "ChatLimitCounter"."value" + 1 END,
                "expiresAt" = CASE WHEN "ChatLimitCounter"."expiresAt" <= (clock_timestamp() AT TIME ZONE 'UTC')
                    THEN (clock_timestamp() AT TIME ZONE 'UTC') + $3::integer * INTERVAL '1 second'
                    ELSE "ChatLimitCounter"."expiresAt" END
            WHERE "ChatLimitCounter"."expiresAt" <= (clock_timestamp() AT TIME ZONE 'UTC')
                OR "ChatLimitCounter"."value" < $2::integer
            RETURNING "value"''',
            key, policy.capacity, policy.seconds,
        )
        if rows:
            return 0
        rows = await self.db.query_raw(
            '''SELECT GREATEST(1, CEIL(EXTRACT(EPOCH FROM
                ("expiresAt" - (clock_timestamp() AT TIME ZONE 'UTC')))))::integer AS retry
            FROM "ChatLimitCounter" WHERE "key" = $1''', key,
        )
        return rows[0]["retry"] if rows else 1

    async def cleanup(self):
        await self.db.execute_raw(
            '''DELETE FROM "ChatLimitCounter" WHERE "key" LIKE 'http:%'
            AND "expiresAt" <= (clock_timestamp() AT TIME ZONE 'UTC')'''
        )
