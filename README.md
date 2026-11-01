# Retrieva

**Turn your PDFs into intelligent AI chatbots for your Platforms.**

Retrieva is a full-stack platform that lets users upload PDF documents, process them into vector embeddings, and create AI chatbots — embeddable on any website via a simple API key + bot ID over WebSockets.

---

## Architecture Overview

```
┌──────────────────┐   Presigned URL      ┌───────────────┐
│  Next.js Admin   │────────────────────►│   AWS S3       │
│  Dashboard       │ (direct PDF upload)  │  (PDF Storage) │
└────────┬─────────┘                      └───────▲───────┘
         │ REST API                               │ Pulls PDF
         ▼                                        │
┌──────────────────────────┐  ┌────────────┐ ┌────┴──────────┐
│  FastAPI Server          │─►│  RabbitMQ  │►│   Consumer    │
│  (REST + WebSocket +     │  │  (Queue)   │ │   Worker      │
│   Producer — single app) │  └────────────┘ └────┬──────────┘
└────┬─────────────▲───────┘                      │ Chunks, Embeds
     │ Prisma      │ Socket.IO                    │ & Stores
     ▼             │ (Bot ID + API Key)    ┌──────▼──────────┐
┌────────────┐  ┌──┴───────────────┐       │   Qdrant DB     │
│ PostgreSQL │  │  Any Website     │       │  (Vector Store)  │
│ (Database) │  │  (end users)     │       └─────────────────┘
└────────────┘  └──────────────────┘
```

> **How it works:** Bot owners manage everything through the admin dashboard.
> Their end users (website visitors) connect directly to the WebSocket server
> using a bot ID + API key — no Retrieva frontend needed.

---

## Tech Stack

| Layer        | Technology                                      |
| ------------ | ----------------------------------------------- |
| Backend API  | FastAPI, Prisma ORM, Python-SocketIO             |
| Database     | PostgreSQL                                       |
| Vector Store | Qdrant                                           |
| Message Queue| RabbitMQ (aio-pika)                              |
| LLM          | Sarvam AI (chat) + OpenAI embeddings via OpenRouter      |
| File Storage | AWS S3 (presigned uploads)                       |
| Auth         | JWT (bcrypt password hashing)                    |

---

## Features

- **One-click demo** — Login and signup offer “Try demo account”, which calls `POST /auth/demo` and opens the dashboard without credentials. The backend automatically creates a dedicated non-admin demo user on first use; later visitors share that workspace and its changes. Use sample documents only. No database migration or demo password configuration is required. Deploy the frontend and backend together for this feature.

- **Bot Builder** — Create multiple AI bots, each with a custom system prompt and isolated document knowledge base.
- **PDF Upload & Processing** — Frontend uploads PDFs directly to S3 via presigned URLs; the consumer worker automatically chunks, embeds, and stores them in Qdrant.
- **Embeddable Chat** — Share a bot ID + API key with anyone; they connect via Socket.IO from **their own website** and let their visitors chat with the bot.
- **RAG Chat** — Each message retrieves relevant document chunks from Qdrant and generates context-aware answers using Sarvam AI.
- **API Key Management** — Generate, list, and revoke API keys per bot to control external access.
- **Chat Sessions** — Persistent conversation history with session management (start, resume, list).
- **Subscription & Plans** — Built-in plan/subscription models for usage limits (bots, documents, requests, tokens).
- **Async Everything** — Fully async backend with FastAPI, aio-pika, and AsyncOpenAI for high throughput.

---

## Producer-Consumer Pipeline

### Producer (FastAPI Server)

When a bot owner uploads a PDF through the dashboard:

1. Client requests a presigned S3 URL from the API.
2. Client uploads the PDF **directly to S3** (server never touches the file).
3. Client calls `POST /documents/{id}/complete` to finalize.
4. The server pushes a message `{ document_id, bot_id, user_id }` to RabbitMQ (`document_process_queue`, durable & persistent).

```
Client → S3 (presigned upload)
Client → POST /documents/{id}/complete
  → Validates ownership & prevents double-enqueue
  → publish_document_job() → RabbitMQ
```

### Consumer (Standalone Worker)

The **Consumer** runs as an independent async process, listening to RabbitMQ:

1. Picks up a job from `document_process_queue` (prefetch = 1).
2. **Pulls the PDF from S3**, extracts text using `pypdf`.
3. Splits text at paragraph and recognized-heading boundaries (up to 1000 characters), reusing unchanged chunks when a document is updated.
4. Generates 1536-dimensional `openai/text-embedding-3-small` embeddings through OpenRouter (batched).
5. Upserts vectors into a Qdrant collection (one collection named by bot ID).
6. Cleans up temp files.

**Error Handling:**
- Non-recoverable errors (404, corrupted PDF, empty content) → message is **rejected** (not requeued).
- Transient errors (network, S3 timeouts) → message is **requeued** for retry.

```
RabbitMQ → on_message()
  → Pull PDF from S3
  → Extract text → Chunk → Embed with OpenRouter
  → Upsert to Qdrant
  → ACK message
```

---

## Project Structure

```
Retrieva_Main/
├── Retrieva/                    # Backend (Python)
│   ├── main.py                  # FastAPI app entrypoint
│   ├── prisma/
│   │   └── schema.prisma        # Database schema
│   ├── src/
│   │   ├── routes/              # REST API endpoints
│   │   │   ├── auth.py          #   Register / Login
│   │   │   ├── bots.py          #   CRUD bots
│   │   │   ├── api_key.py       #   API key management
│   │   │   ├── documenation.py  #   Document upload flow
│   │   │   ├── chats.py         #   Chat session/message history
│   │   │   └── testing.py       #   Bot testing endpoint
│   │   ├── sockets/
│   │   │   ├── ws_chats.py      #   Socket.IO server setup
│   │   │   └── handlers.py      #   WebSocket event handlers
│   │   ├── producer/
│   │   │   ├── producer.py      #   RabbitMQ job publisher
│   │   │   └── rabbit_mq.py     #   RabbitMQ connection
│   │   ├── llm/
│   │   │   ├── llm.py           #   Sarvam chat completion
│   │   │   └── prompt/          #   System prompts
│   │   ├── vector/              #   Qdrant retrieval (query-side)
│   │   └── utils/               #   JWT, S3, password, API key utils
│   ├── consumer/                # Consumer Worker (standalone)
│   │   ├── consumer.py          #   Main consumer loop
│   │   ├── rabbit_mq.py        #   RabbitMQ connection
│   │   ├── aws/
│   │   │   └── file_loader.py   #   S3 PDF loader
│   │   └── vector/
│   │       ├── insert.py        #   Chunk, embed, upsert to Qdrant
│   │       ├── qdrantdb.py      #   Qdrant client setup
│   │       └── vector.py        #   Collection management
│   └── requirements.txt
│
└── Retrieva-FE/                 # Admin Dashboard (Next.js — separate repo)
```

---

## Environment Variables

For a fresh setup, copy `Retrieva/env.example` to `Retrieva/.env` and replace the
placeholder values. Both the backend and consumer use this file:

```env
DATABASE_URL=postgresql://...          DIRECT_URL=postgresql://...
JWT_SECRET=your-jwt-secret             ACCESS_TOKEN_EXPIRE_MINUTES=1440
AWS_REGION=us-east-1                   AWS_S3_BUCKET=your-bucket
AWS_ACCESS_KEY_ID=...                  AWS_SECRET_ACCESS_KEY=...
RABBIT_MQ_URL=amqp://...              RABBIT_MQ_API_KEY=...
SARVAM_API_KEY=your-sarvam-api-key
OPENROUTER_API_KEY=your-openrouter-api-key
SARVAM_BASE_URL=https://api.sarvam.ai/v2
SARVAM_MODEL=sarvam-105b
QDRANT_DB_CLUSTER_URL=https://...      QDRANT_DB_API_KEY=...
```

`SARVAM_API_KEY` is required by the backend for answers and query rewriting.
The existing OpenAI SDK connects to Sarvam's OpenAI-compatible endpoint;
chat requests are sent to Sarvam. No OpenAI key or credits are required.
The `openai` Python dependency is only the compatible client for Sarvam chat.
Both services read `Retrieva/.env`, independent of the working directory.

Document and query embeddings call `https://openrouter.ai/api/v1/embeddings`
with `openai/text-embedding-3-small`, explicitly requesting 1536 dimensions.
Set `OPENROUTER_API_KEY` in `Retrieva/.env`; both the API and consumer require it.
There is no model download, no local GPU requirement, and no direct OpenAI or
Gemini key needed. Embedding requests consume your OpenRouter balance.

The current listed embedding price is $0.02 per million input tokens (checked
October 29, 2026). Indexing document chunks and embedding search queries both
cost tokens. Re-indexing costs again; stored vectors can be searched repeatedly
without re-embedding the documents. Sarvam chat and query rewriting are billed
separately. See [OpenRouter pricing](https://openrouter.ai/openai/text-embedding-3-small).

Original bot-ID collections generated with `text-embedding-3-small` at 1536
dimensions are reused directly, with no migration or re-embedding charge.
Gemini or BGE-M3 vectors cannot be reused with this model. If you have documents
in a collection from one of those models, stop the API and pause uploads, then
copy and re-embed that collection's chunks from the `Retrieva/` directory:

```bash
python -m consumer.utils.reindex_embeddings --bot-id YOUR_BOT_UUID --source-collection OLD_COLLECTION
```

This optional command preserves source collections, chunk IDs, and metadata;
it consumes OpenRouter embedding credits. It can be rerun after an interruption
but resubmits embeddings, incurring their cost again. It rejects identical
source and target collections to prevent needless re-embedding of original
OpenAI vectors. Re-upload PDFs if no stored chunks remain. Resume the API once
migration completes. Migration does not run automatically during startup.

---

## Getting Started

```bash
# 1. Backend (Producer API)
cd Retrieva && pip install -r requirements.txt
prisma generate && prisma db push
fastapi dev main.py                 # → http://localhost:8000

# 2. Consumer Worker (separate terminal)
cd Retrieva && pip install -r consumer/requirements.txt
python -m consumer.consumer
```

> The frontend (Next.js admin dashboard) lives in `Retrieva-FE/` — see its own README for setup.

---

## API Endpoints

| Method | Endpoint                              | Description                  |
| ------ | ------------------------------------- | ---------------------------- |
| POST   | `/auth/register`                      | Register a new user          |
| POST   | `/auth/login`                         | Login & get JWT token        |
| POST   | `/bots`                               | Create a bot                 |
| GET    | `/bots`                               | List user's bots             |
| PUT    | `/bots/{bot_id}`                      | Update a bot                 |
| DELETE | `/bots/{bot_id}`                      | Delete a bot                 |
| POST   | `/bots/{bot_id}/api-keys`             | Generate API key             |
| GET    | `/bots/{bot_id}/api-keys`             | List API keys                |
| DELETE | `/api-keys/{id}`                      | Revoke an API key            |
| POST   | `/bots/{bot_id}/documents/upload-url` | Get presigned upload URL     |
| POST   | `/documents/{id}/complete`            | Trigger document processing  |
| GET    | `/chats/sessions/{bot_id}`            | List chat sessions           |
| GET    | `/chats/sessions/{id}/messages`       | Get session messages         |
| POST   | `/testing/{bot_id}`                   | Test bot with a question     |

### WebSocket Events (Socket.IO)

| Event              | Direction | Description                        |
| ------------------ | --------- | ---------------------------------- |
| `connect`          | Client→   | Authenticate with API key + bot ID |
| `start_chat`       | Client→   | Start a new chat session           |
| `resume_chat`      | Client→   | Resume an existing session         |
| `user_message`     | Client→   | Send a message                     |
| `chat_started`     | →Client   | Session created confirmation       |
| `chat_resumed`     | →Client   | Session resumed confirmation       |
| `assistant_message`| →Client   | AI response                        |

### Embeddable Chat — How End Users Connect

Retrieva is a **BaaS (Bot as a Service)**. The dashboard is for bot owners only. End users connect from **any website** using Socket.IO:

```js
// On the bot owner's website — their visitors chat with the bot
const socket = io("https://your-retrieva-server", {
  auth: { apiKey: "fm_...", botId: "uuid-..." }
});

socket.emit("start_chat");
socket.on("chat_started", ({ sessionId }) => { /* ready */ });
socket.emit("user_message", { content: "How do I...?" });
socket.on("assistant_message", ({ content }) => { /* AI reply */ });
```

---

## Database Models

- **User** — Accounts with email/password auth
- **Bot** — AI bots with custom system prompts
- **ApiKey** — Hashed keys for bot access
- **Document** — Uploaded PDFs with processing status (`queued → processing → ready/failed`)
- **ChatSession / ChatMessage** — Conversation history
- **Plan / Subscription** — Usage-based billing
- **UsageMetric** — Per-key daily request/token tracking

## Updating PDFs without embedding every chunk again

The worker now uses deterministic paragraph and recognized-heading boundaries,
with a 1,000-character maximum. Long paragraphs split at sentence/word boundaries.
It does not add overlap from unrelated neighboring paragraphs or call an AI model
to choose boundaries. PDF layout extraction preserves paragraph spacing where
possible; PDFs with poor layout or changed pagination can still produce changed
chunks even when some visible wording is unchanged.

In **My assistants → Manage documents / update PDF**, choose **Update PDF** next
to the existing document. This keeps its document ID even if the filename changes.
**Add another PDF** creates a separate document; matching filenames do not replace
one another automatically.

The worker compares exact chunk text within that document and embedding model.
Unchanged chunks retain their Qdrant point IDs and vectors; only new text is sent
to OpenRouter. Repeated identical chunks retain their separate occurrences but
share one embedding request per unique new text. File/page/section metadata is
versioned separately. Changes to the embedding model require new embeddings.

Uploads get separate S3 objects and revision records. A PostgreSQL advisory lock
serializes writers for each document across worker processes. Retrieval filters
by the document's active revision: a staged upload remains hidden until its
revision pointer commits. Obsolete vectors are deleted after activation. Failed
embedding requests leave the previous active revision searchable. Duplicate jobs
reuse staged vectors where available, and older queued updates cannot overwrite
a newer active revision. Previously stored S3 versions are retained.

### Required upgrade steps

Stop the API and worker, then run from `Retrieva/` with `.venv` activated:

```bash
python -m pip install -r requirements.txt -r consumer/requirements.txt
python -m prisma migrate deploy
python -m prisma generate
```

Restart the API and worker, and deploy/restart the frontend. No automatic
re-embedding or deletion happens during this migration. Existing collections
remain readable; on their first update, chunks whose text differs because of
the new splitting rules will need new embeddings. Qdrant creates the payload
indexes needed for revision filters automatically; its API key needs write access.

Offline checks (no AWS/RabbitMQ/OpenRouter credentials or spending required):

```bash
python -m unittest discover -s tests
```

## Token, cost, and latency benchmarking

The API and document worker emit JSON benchmark records to the terminal and
`Retrieva/logs/benchmarks-<process-id>.jsonl`. Files rotate at 10 MiB with five
backups per process and are excluded from Git. No prompts, replies, document text,
API keys, or raw provider error bodies are included in these records.

- `provider_call`: Sarvam `query_rewrite` and `answer` calls, plus each OpenRouter
  embedding batch/attempt. Records include provider completion/request IDs,
  model, reported input/output/total/cached/reasoning tokens, duration in
  milliseconds, and cost fields. Sarvam SDK call duration includes its automatic
  retries, but usage covers only the returned response. Unobserved retry usage
  cannot be inferred. OpenRouter retry attempts are logged individually.
- `benchmark_summary`: one record per chat turn or document indexing job.
  `trace_id` links its provider calls to the summary. Chat summaries include
  session/bot/message IDs, rewrite/retrieval/answer/database-delivery stage times,
  total server time, observed provider token/cost sums, and success/fallback/error
  outcomes. Parallel retrieval calls share the trace; their durations overlap.

**Tokens come directly from provider `usage` fields.** There is no local tokenizer,
word-count heuristic, or text-length estimate. Missing fields stay `null`.
Summary fields named `reported_*` sum only observed counters; check
`usage_complete`, `usage_missing_calls`, and `usage_incomplete_calls` before using
those sums as complete billing totals. Reasoning tokens are already included in
reported completion tokens and are never charged twice by the estimate.

Sarvam's documented completion response reports tokens, not a final rupee charge.
`estimated_cost` is therefore explicitly an INR estimate, excluding tax, using
reported tokens and configurable prices. Defaults for `sarvam-105b` and
`sarvam-105b-conversations` are ₹15 input / ₹5 cached input / ₹60 output per
million tokens, checked on 2026-10-30 against
[Sarvam pricing](https://docs.sarvam.ai/api/getting-started/pricing).
If cached usage is absent, the record says `cache_assumption=all_input_uncached`;
this is not a provider-reported zero cache count. Unknown models/missing required
usage produce no estimate. For OpenRouter, `reported_cost` preserves `usage.cost`
in USD when returned; missing cost is not manufactured. INR and USD are never
combined into one monetary total. Your provider dashboard/invoice remains the
source of truth for billed charges and plan-specific rates.

Optional settings in `Retrieva/.env` (restart the API and worker after changing):

```env
BENCHMARK_ENABLED=true
BENCHMARK_LOG_TO_CONSOLE=true
BENCHMARK_LOG_PATH=logs/benchmarks-{pid}.jsonl
SARVAM_PRICING_MODEL=sarvam-105b
SARVAM_INPUT_PRICE_PER_1M_TOKENS=15
SARVAM_CACHED_INPUT_PRICE_PER_1M_TOKENS=5
SARVAM_OUTPUT_PRICE_PER_1M_TOKENS=60
```

Use your account's actual rates if different. Set `BENCHMARK_LOG_PATH=` to disable
file output, or `BENCHMARK_ENABLED=false` to disable benchmark output entirely.

```bash
# From Retrieva/, after making some requests:
tail -f logs/benchmarks-*.jsonl

# With jq installed, inspect one process's chat summaries:
jq 'select(.event == "benchmark_summary" and .operation == "chat_turn") |
    {trace_id, outcome, server_total_ms, stages_ms, providers}' logs/benchmarks-12345.jsonl
```

`server_total_ms` ends after saving/emitting the answer; it excludes browser
network delivery/rendering. Provider durations include request/response overhead.
`effective_output_tokens_per_second` uses reported completion tokens divided by
whole call duration; it is not isolated model decoding speed. This non-streaming
chat flow does not measure time to first token. Benchmark instrumentation does
not make additional provider requests or spend credits by itself.

### Low-latency streaming chat

The chat path now uses one embedding search (the question plus the last two user
questions for follow-ups), followed by one Sarvam answer call. It no longer waits
for the four-query LLM rewrite. Revision filtering still runs against current
stored document revisions on every search. Collection index setup is reused
within each backend process. Answers use `reasoning_effort="none"` and an output
limit of 768 tokens. This favors ordinary customer questions; evaluate retrieval
quality with your own questions, especially topic changes and complex questions.

The frontend sends `user_message` with `{content, stream: true}`. Integrations
opting into streaming should append `assistant_delta.content`, grouped by
`sessionId` and `messageId`. The final `assistant_message` has `done: true` and
the complete content: replace the partial reply with it rather than appending a
second message. Clients omitting `stream` keep the complete-message protocol.
Only visible answer content is streamed; reasoning is never sent to clients.
If a stream fails after partial output, the final reply preserves the partial
text and adds an interruption notice. Final provider usage is logged when
available; interrupted streams may have unknown usage.

`provider_ttft_ms` measures the Sarvam call until its first visible answer text.
`server_ttft_ms` on a streaming `chat_turn` summary measures from receiving the
question until emitting its first answer text, including search and database
work. Neither measures browser delivery or rendering. Total generation time is
still logged separately. Streaming usage comes from the provider's final usage
chunk, never a local token count.

A small live check on 2026-10-30 using one indexed bot, five retrieved chunks and
the same synthetic question three times measured search-plus-answer first text
at 2.565s, 1.862s and 2.180s, with zero reasoning tokens. These checks excluded
Socket.IO delivery, chat-history reads and message persistence, and are not a
production percentile or a latency guarantee. Restart the backend and reload
the frontend, then measure `server_ttft_ms` on representative real chats to
validate the 3–4 second target under actual load.

Search timing fields are also separated: `retrieval_prepare` (database and
collection/index checks), `query_embedding` (OpenRouter), and `qdrant_search`
(the vector query). These are parts of the parent `retrieval` stage; do not add
both parent and child stages together. A fresh-process live check measured
0.275s preparation, 1.747s embedding, and 0.282s Qdrant search.

### Chat rate limits and daily output allowance

The daily output allowance is the hard constant `DAILY_OUTPUT_TOKEN_LIMIT =
50_000` in `src/sockets/limits.py`, per bot across every key and visitor. A day
resets at midnight **Asia/Kolkata**. It counts provider-reported completion
(output) tokens. Input tokens and embeddings have separate costs; this output
allowance is not a total spending cap.

Current limits:

| Control | Allowance |
| --- | --- |
| Output tokens per bot per India calendar day | 50,000 |
| Output per reply | Up to 768, reduced to the remaining daily allowance |
| Messages per chat session | 10/minute |
| Messages per verified client IP | 20/minute |
| Messages per API key | 60/minute |
| Messages per bot across keys | 120/minute |
| Active replies per chat session / IP / bot / whole backend | 1 / 2 / 5 / 20 |
| Connection attempts per IP, including failed authentication | 20/minute |
| Open sockets per IP / key / whole backend | 5 / 100 / 500 |
| Start/resume operations per IP / key | 20 / 60 per minute |
| User message length | 4,000 characters |
| Socket.IO packet size | 32 KiB |
| Maximum complete reply operation | 90 seconds |

Minute limits use fixed windows, so bursts can cross minute boundaries. IP
limits are shared by visitors on the same network. Connection attempts are
limited at Socket.IO authentication; also configure your ingress/reverse proxy
for transport-level request floods. Configure Uvicorn's trusted proxy addresses
explicitly when deployed behind a proxy. The app ignores arbitrary forwarded
headers and uses the server-verified ASGI client address.

PostgreSQL counters and atomic reservations work across processes and restarts;
Redis is not required. Admission is one database round trip using a small
transaction-locked database function, with no provider requests while holding the
lock. Every message rechecks the API key, active bot, and chat's key ownership.
Revoked keys cannot continue generating on an existing socket. Switching keys
cannot bypass the shared bot quota. The old unmetered `/testing/{bot_id}` endpoint
now returns HTTP 410 and makes no provider call.

An admitted request reserves its maximum output count before any embedding or
LLM call. Completion releases the unused portion using the provider's reported
usage, not a local tokenizer. Failed/interrupted LLM calls with no reported usage
keep their full reservation charged; failures before an LLM call refund it.
Expired/crashed requests release their concurrency slot after 150 seconds but
keep their token reservation charged. Reservations spanning midnight settle
against their original day. SDK automatic retries are disabled to prevent hidden
additional generation calls. If the database cannot enforce admission, the chat
rejects the request rather than making an unmetered provider call.

Connections renew a 90-second lease every 30 seconds, and disconnect releases
the lease. Unexpected process termination therefore does not permanently consume
connection slots. A failed renewal disconnects the client. Generations have a
longer lease than their 90-second timeout.

Rejected operations emit `chat_error` to the requesting socket:

```json
{"code":"rate_limited","message":"Too many requests. Please wait and try again.","retryAfter":25}
```

Other codes include `daily_token_limit`, `reply_in_progress`, `busy`,
`connection_limit`, `message_too_long`, `unauthorized`, and `unavailable`.
Connection refusals carry the same error details through Socket.IO
`connect_error`. The frontend exits its waiting state and displays an error;
rate-limit messages are not saved as assistant replies. Benchmark summaries log
`outcome: "rejected"` and `rejection_code` without paid provider calls. Active
replies log `reserved_output_tokens`; a failed final settlement logs
`quota_settlement_error` and leaves the reservation charged.

For another installation, apply the migration before restarting the API:

```bash
python -m prisma migrate deploy
python -m prisma generate
```

The database login must own the internal limit tables/function (the usual same
migration/application login), or receive explicit privileges. The new tables
have row-level security enabled and the admission function is not publicly
executable. They are internal accounting data, not public APIs.

Database concurrency tests are opt-in and run only against a disposable local
PostgreSQL on port 55433. Apply all migrations there first, then run:

```bash
RETRIEVA_LIMIT_TEST_DATABASE_URL='postgresql://postgres:local-test-only@127.0.0.1:55433/retrieva_limits_clean' \
  python -m unittest discover -s tests
```


## Retrieva rename

The backend folder is now `Retrieva/` and the dashboard is `Retrieva-FE/`.
Before starting the updated backend against an existing database, apply the
function-rename migration from `Retrieva/`:

```bash
python -m prisma migrate deploy
python -m prisma generate
```

The migration preserves the admission function's implementation and privileges.
Historical migrations keep their original names and contents for checksum integrity.
Use `RETRIEVA_LIMIT_TEST_DATABASE_URL` for optional disposable-database tests.
Existing database URLs, cloud resource names, and Git remote URLs remain valid;
the rename does not require moving data or renaming hosted repositories.
