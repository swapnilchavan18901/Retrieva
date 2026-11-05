# Retrieva launch runbook

This release adds abuse controls and delivery safeguards. A passing test suite is
not a production launch approval: complete the deployment and product gates below
before accepting customer documents or charging customers.

## Safeguards implemented

| Control | Default behavior |
| --- | --- |
| REST requests | 120 per IP per 60-second window, configurable |
| Login | 10 per IP per minute; 20 per normalized email per 15 minutes across IPs |
| Registration | 5 per IP per hour |
| Shared demo | Enabled in every deployment by code constants; shared sample workspace |
| Demo attempts | 5 per IP per minute |
| Upload URL creation | 15 per IP per minute |
| REST JSON bodies | Maximum 64 KiB; chunked bodies checked; 15-second body timeout |
| Resource caps | 10 bots per account, 50 documents per bot, 10 active API keys per bot |
| PDF processing | 50 MiB actual download, 500 pages, 2 million extracted characters |
| Chat | Existing shared connection, request, concurrency and daily output token limits |
| Document delivery | Transactional PostgreSQL outbox, confirmed publication, idempotent worker processing |
| Worker retries | Up to 3 automatic retries; permanent/exhausted failures sent to a persistent dead-letter queue |

HTTP counters live in PostgreSQL, so restarting an API process does not reset them.
Concurrent quota checks use transaction locks. Windows reset after their stored
expiry; they are not sliding windows, so boundary bursts remain possible. Visitors
sharing a NAT share IP allowances; targeted login throttles can temporarily block
an account under attack. Keep an ingress/WAF rate limit in front of the application.
The limiter returns `429` with `Retry-After`. A limiter database failure returns
`503` and denies the request. `/` (liveness), `/health` (database readiness), CORS
preflight and Socket.IO transport requests bypass REST throttling. Socket.IO has
its own connection/message limits and a 32 KiB transport payload ceiling.

Registration returns only public user fields, checks bcrypt's 72-byte UTF-8 limit,
handles duplicate races as `409`, and runs password work off the event loop. Invalid
credentials return `401`. API-key revocation disables keys and preserves chat
references. Revoked keys cannot start/resume sessions or admit new generations;
an already admitted reply can finish. Revoked records do not use active-key quota.

Responses include generated `X-Request-ID` values, private REST responses are not
cached, and validation errors omit input values. Internal errors return generic
messages. These controls do not replace a secret scrubber for infrastructure or
provider logs. Production disables API documentation routes.

## Configure and deploy

1. Use separate production PostgreSQL, S3, RabbitMQ and Qdrant resources. Demo
   access is enabled in every deployment, including production. It uses a dedicated
   shared non-admin account; use sample files in that workspace.
2. Copy `env.example` to a protected `Retrieva/.env` and replace placeholders.
   Set `ENVIRONMENT=production`, a random `JWT_SECRET` of at
   least 32 characters, and explicit HTTPS origins in `CORS_ORIGINS` and
   `SOCKET_CORS_ORIGINS`. Add approved customer embedding domains to the latter.
   AWS settings, including an optional session token, come from this file rather
   than unrelated shell AWS credentials. Deploy the complete `shared/` package
   with both the API and worker. Never commit the completed `.env` file.
3. Configure `HTTP_REQUESTS_PER_MINUTE`, `MAX_BOTS_PER_USER`,
   `MAX_DOCUMENTS_PER_BOT` and `MAX_API_KEYS_PER_BOT` for your pilot. These caps
   apply to all accounts; subscription-aware billing enforcement is not implemented.
4. Create a fresh Python 3.11 environment, install the pinned requirements, generate the Prisma client, and
   apply migrations to the intended database before deploying this API:

   ```sh
   python -m pip install -r requirements.txt -r consumer/requirements.txt
   prisma generate
   prisma migrate deploy
   ```

   The API and worker share `requirements.txt`; `consumer/requirements.txt`
   includes the same lock. `requirements.in` records direct dependencies. Refresh
   deliberately with `uv pip compile requirements.in -o requirements.txt
   --python-version 3.11 --upgrade`, audit the lock and rerun all checks. Deploy
   from a clean environment so removed dependencies do not linger. JWT handling
   now uses PyJWT and still accepts previously issued signed HS256 tokens with
   `sub` and `exp` claims.

   `20261103090000_document_outbox` is required. It adds a table without modifying
   customer records. Back up first. Test migrations against a restored staging
   database. Never use `prisma migrate reset` on customer data.
5. Run `uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1` and a separate
   `python -m consumer.consumer` service with automatic restarts. Keep the API
   running: its lifespan tasks dispatch outbox jobs and clean expired HTTP counters.
   The Socket.IO room manager is in-process: use a single API worker initially.
   Add a shared Socket.IO manager and appropriate routing before adding replicas.
6. Put TLS and ingress request/connection limits in front of the API. Configure
   Uvicorn proxy-header trust for the exact ingress IPs/CIDRs and have ingress
   replace incoming forwarded headers. Do not trust arbitrary forwarded headers
   or expose a proxy-trusting backend directly to the Internet. Apply HTTPS/HSTS
   at the edge. Apply worker memory/CPU limits: PDF parsing still processes
   untrusted files and page/text checks do not prevent every decompression attack.
7. Build the frontend with `NEXT_PUBLIC_API_URL` pointing to the production API.
   Demo visibility is controlled by a code constant. Browser public variables are build-time
   values. Run `pnpm lint`, `pnpm typecheck`, `pnpm build`, then `pnpm start`.
   Deploy frontend and backend together for the API-key creation ID response.
8. Restrict S3 bucket access and CORS to required operations/origins. Set upload
   lifecycle rules for abandoned uploads and old revisions. A presigned PUT URL
   can be reused until it expires; processing limits are enforced in the worker,
   not as a pre-transfer S3 byte quota. Put storage budgets and alerts in place.

## Smoke test before inviting users

- Create a fresh account and confirm the response contains no password hash.
  Test duplicate signup, wrong passwords and multibyte/oversized passwords.
- Use a second account to attempt access to another account's bots, documents,
  API keys and chat sessions; confirm denials.
- Trigger a REST throttle and confirm `429`, `Retry-After` and browser-readable
  error feedback. Confirm ingress supplies distinct verified client IPs.
- Create a bot, issue a key, upload a small PDF, wait for indexing and chat. Replace
  the PDF and confirm failed updates preserve the previous indexed revision.
- Stop RabbitMQ, complete an upload and confirm its outbox record remains. Restore
  RabbitMQ and confirm dispatch resumes. Invalid PDFs should be marked failed
  and reach `document_process_dead_letter_queue` without endless retries.
- Revoke a key after using it, then confirm new messages and session controls are
  denied. Delete a bot with chat history and confirm the database operation succeeds.
- Verify `/health` returns `503` when the DB cannot answer and `/` stays live.
  The health endpoint does not certify RabbitMQ, Qdrant or paid provider availability.
- Restore a backup in staging and complete the same upload/chat flow.

## Monitor and recover

Alert on HTTP `5xx`/`429` spikes, database connections and disk, queue depth and
consumer health, dead-letter messages, Qdrant storage, and provider/storage spend.
Correlate customer reports with `X-Request-ID`; do not request passwords or raw keys.
Track outbox backlog with:

```sql
SELECT COUNT(*) AS pending, MIN("createdAt") AS oldest, MAX("attempts") AS attempts
FROM "DocumentJobOutbox";
```

The outbox retries broker outages with capped backoff; it is not a dead-letter
queue. A crash after publish but before deleting the outbox row can redeliver a
job. Revision-based processing handles duplicate delivery. Monitor a growing
backlog and keep a current dispatcher running until it drains during rollback.
Worker failures retry 3 times and then preserve the original payload in
`document_process_dead_letter_queue`. Inspect and correct the cause before
re-publishing; do not bulk replay poison jobs. Replays must retain ownership IDs.
Existing uploads stranded in `queued` before this migration need operator review;
the migration deliberately does not enqueue possibly unfinished old uploads.

## Remaining gates for a commercial public launch

- Choose and implement email verification, password reset and transactional email.
  Add signup abuse controls such as verified invitations or a CAPTCHA before a
  broad public signup campaign; per-IP limits alone do not stop distributed abuse.
- Implement payment checkout, verified/idempotent webhooks, cancellations,
  subscription entitlements and customer billing support before charging.
  Plan/subscription tables currently do not provide these behaviors. Provider
  budgets must also cover embeddings, input tokens and document replacements;
  the existing daily chat allowance caps output tokens rather than total spend.
- Define data retention/export/deletion behavior and publish accurate customer
  policies. Deleting a bot now removes DB history but **does not purge S3 objects
  or Qdrant vectors**. Complete external-storage deletion and account closure
  before promising full erasure to customers.
- Complete dependency/security review, load testing and a restore drill. Rotate
  any credentials ever exposed in logs or Git. Wire production error reporting,
  uptime alerts, ownership/on-call and a customer support channel.
- Move browser bearer tokens/API keys out of persistent local storage or document
  and mitigate the XSS exposure. Add a CSP tested against the PDF viewer and app
  dependencies. Add pagination for large chat histories and sessions.
- Pin and regularly update worker dependencies; isolate PDF processing. Add OCR
  only after controlling its resource cost. Encrypted/scanned PDFs currently fail
  with a controlled processing error.

CI workflows in both repositories run the relevant checks. Backend CI provisions
disposable PostgreSQL on port 55433 and runs concurrency tests as well as offline
tests; no production database or paid provider calls are required.

Validated locally on November 3, 2026: 137 backend tests passed in a clean Python
environment, including PostgreSQL concurrency/delivery tests; 9 frontend tests,
zero-warning lint, TypeScript and the production build passed. Prisma validated
and all 7 migrations applied to disposable PostgreSQL. Production login/signup
pages rendered in the browser. Audits of the
backend lock and frontend production dependency lock reported no known
vulnerabilities. Live paid-provider, email, payment and production infrastructure
flows were not exercised. Temporary test services were stopped afterward.
