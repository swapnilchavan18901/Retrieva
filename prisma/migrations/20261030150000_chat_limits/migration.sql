CREATE TABLE "ChatLimitCounter" (
    "key" TEXT PRIMARY KEY,
    "value" INTEGER NOT NULL DEFAULT 0,
    "expiresAt" TIMESTAMP(3) NOT NULL
);
CREATE INDEX "ChatLimitCounter_expiresAt_idx" ON "ChatLimitCounter"("expiresAt");
CREATE TABLE "ChatLimitLease" (
    "id" TEXT PRIMARY KEY,
    "kind" TEXT NOT NULL,
    "botId" TEXT NOT NULL,
    "apiKeyId" TEXT NOT NULL,
    "sessionId" TEXT NOT NULL,
    "ipHash" TEXT NOT NULL,
    "expiresAt" TIMESTAMP(3) NOT NULL,
    "dayKey" TEXT,
    "reservedTokens" INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX "ChatLimitLease_kind_expiresAt_idx" ON "ChatLimitLease"("kind", "expiresAt");

-- One database round trip for admission; locks never span provider/network calls.
CREATE FUNCTION filemind_admit_chat(p_id TEXT, p_bot TEXT, p_key TEXT, p_session TEXT,
                                    p_ip TEXT, p_max INTEGER, p_daily INTEGER)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    v_now TIMESTAMP;
    v_day TEXT;
    v_reset TIMESTAMP;
    v_window TEXT;
    v_retry INTEGER;
    v_used INTEGER;
    v_reserve INTEGER;
    v_keys TEXT[];
    v_caps INTEGER[] := ARRAY[10,20,60,120];
    v_count INTEGER;
    i INTEGER;
BEGIN
    PERFORM set_config('lock_timeout', '2000ms', true);
    PERFORM pg_advisory_xact_lock(438215709);
    v_now := clock_timestamp() AT TIME ZONE 'UTC';
    DELETE FROM "ChatLimitLease" WHERE "expiresAt" <= v_now;
    DELETE FROM "ChatLimitCounter" WHERE "expiresAt" <= v_now;
    IF NOT EXISTS (
        SELECT 1 FROM "ChatSession" s JOIN "Bot" b ON b."id"=s."botId"
        JOIN "ApiKey" k ON k."id"=p_key
        WHERE s."id"=p_session AND s."botId"=p_bot AND s."apiKeyId"=p_key
        AND k."botId"=p_bot AND k."isActive" AND b."isActive"
    ) THEN RETURN jsonb_build_object('code','unauthorized','retry',0); END IF;
    IF EXISTS (SELECT 1 FROM "ChatLimitLease" WHERE "kind"='generation' AND "sessionId"=p_session)
    THEN RETURN jsonb_build_object('code','reply_in_progress','retry',2); END IF;
    SELECT count(*) INTO v_count FROM "ChatLimitLease" WHERE "kind"='generation';
    IF v_count >= 20 OR
       (SELECT count(*) FROM "ChatLimitLease" WHERE "kind"='generation' AND "botId"=p_bot) >= 5 OR
       (SELECT count(*) FROM "ChatLimitLease" WHERE "kind"='generation' AND "ipHash"=p_ip) >= 2
    THEN RETURN jsonb_build_object('code','busy','retry',5); END IF;
    v_window := floor(extract(epoch FROM v_now)/60)::bigint::text;
    v_reset := to_timestamp((v_window::bigint+1)*60) AT TIME ZONE 'UTC';
    v_retry := GREATEST(1, ceil(extract(epoch FROM (v_reset-v_now)))::integer);
    v_keys := ARRAY['messages_session:'||p_session||':'||v_window,
                    'messages_ip:'||p_ip||':'||v_window,
                    'messages_key:'||p_key||':'||v_window,
                    'messages_bot:'||p_bot||':'||v_window];
    FOR i IN 1..4 LOOP
        SELECT "value" INTO v_used FROM "ChatLimitCounter" WHERE "key"=v_keys[i];
        IF COALESCE(v_used,0) >= v_caps[i]
        THEN RETURN jsonb_build_object('code','rate_limited','retry',v_retry); END IF;
    END LOOP;
    FOR i IN 1..4 LOOP
        INSERT INTO "ChatLimitCounter" ("key","value","expiresAt") VALUES (v_keys[i],1,v_reset)
        ON CONFLICT ("key") DO UPDATE SET "value"="ChatLimitCounter"."value"+1;
    END LOOP;
    v_day := 'output:'||p_bot||':'||((v_now AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date::text;
    v_reset := ((((v_now AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date+1)::timestamp
                    AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'UTC';
    SELECT "value" INTO v_used FROM "ChatLimitCounter" WHERE "key"=v_day;
    v_reserve := LEAST(p_max, p_daily-COALESCE(v_used,0));
    IF v_reserve <= 0
    THEN RETURN jsonb_build_object('code','daily_token_limit','retry',GREATEST(1,ceil(extract(epoch FROM (v_reset-v_now)))::integer)); END IF;
    INSERT INTO "ChatLimitCounter" ("key","value","expiresAt") VALUES (v_day,v_reserve,v_reset+INTERVAL '5 minutes')
    ON CONFLICT ("key") DO UPDATE SET "value"="ChatLimitCounter"."value"+v_reserve;
    INSERT INTO "ChatLimitLease" ("id","kind","botId","apiKeyId","sessionId","ipHash","expiresAt","dayKey","reservedTokens")
    VALUES (p_id,'generation',p_bot,p_key,p_session,p_ip,v_now+INTERVAL '150 seconds',v_day,v_reserve);
    RETURN jsonb_build_object('id',p_id,'max_tokens',v_reserve);
END;
$$;

-- Internal accounting only; never expose counters through public database APIs.
ALTER TABLE "ChatLimitCounter" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "ChatLimitLease" ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE "ChatLimitCounter", "ChatLimitLease" FROM PUBLIC;
REVOKE ALL ON FUNCTION filemind_admit_chat(TEXT,TEXT,TEXT,TEXT,TEXT,INTEGER,INTEGER) FROM PUBLIC;
