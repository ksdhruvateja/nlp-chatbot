# FixBridge AI Repair Service

An implementation of the FixBridge repair-diagnosis flow: a homeowner describes a problem and uploads photos, video or documents. They get back an evidence-based DIY plan, a safety warning, follow-up questions, or a recommendation to call a professional. Every answer cites approved repair knowledge.

Stack: **Python / FastAPI**, **PostgreSQL 16 + pgvector** (Neon in production), encrypted media storage (local disk or **S3**), a DB-backed job queue, and plain-JS UI. AWS deployment: see `deploy/aws/README.md`.

## How the diagram maps to code

| Diagram phase | Where |
|---|---|
| Knowledge ingestion (prepared in advance) | `app/ingestion.py`, `POST /api/admin/knowledge`, `scripts/seed.py` |
| 1 – User input | `app/static/` (drop-in UI, or call the API from the existing FixBridge site) |
| 2 – API & file processing | `POST /api/repair-requests` in `app/main.py`, `app/storage.py`, `app/security.py` |
| 3 – Context preparation | `app/pipeline/context.py` (property passport, doc text/OCR, image resize, video frames + audio, facts) |
| 4 – Repair knowledge retrieval | `app/pipeline/retrieval.py` (pgvector semantic + tsvector keyword, brand/model/version filters, ranking, citations) |
| 5 – AI orchestration | `app/ai/orchestrator.py`, `app/ai/adapters.py` (OpenAI/Gemini cloud → local Ollama fallback → controlled failure) |
| 6 – Evidence validation & safety | `app/pipeline/safety.py` (ref verification, sufficiency, deterministic safety rules, customer choice) |
| Clarification loop | `generate_questions` + `POST /api/repair-requests/{id}/clarifications` (round limit → professional) |
| 7 – Repair plan & cost | `app/pipeline/repair_plan.py` (prerequisites, tools, compatible products, verified prices, cost assumptions) |
| 8 – Save & show | `runner.save_version` (versioned `diagnoses`), `api_models.DiagnosisResponse` validation |
| 9 – Outcome & property history | `POST /api/repair-requests/{id}/outcome`, `/api/admin/outcomes/{id}/review`, `/api/admin/eval-dataset` |
| Cross-cutting | access control, Fernet-encrypted media, rate limits, idempotency keys, retries/backoff, circuit breakers, `/health/ready`, `model_calls` cost tracking, `audit_log`, `alerts` |

The orchestration between phases lives in `app/pipeline/runner.py`.

## Real AI: Google Gemini

See [Gemini setup and live validation](deploy/gemini.md) for the real diagnosis,
image analysis, video-audio transcription and semantic retrieval pipeline.
Use `compose.gemini.yml` with secure credentials; `compose.demo.yml` continues to
provide a separate offline demonstration. Model weights are hosted by Google.

## Local demo (no API keys required)

Run `docker compose -f compose.demo.yml up --build -d`. This starts PostgreSQL
with pgvector, seeds illustrative knowledge and a demo customer, and runs the
API plus its background worker. Visit port 8000 and use **Dev login** with
`demo@example.com`. Credentials in this stack are local demo values only;
the port binds to loopback. Stop with `docker compose -f compose.demo.yml down`;
add `-v` only to delete the database and uploaded media.

The demo uses deterministic templates, hash embeddings and sample prices. It
does not provide model reasoning or production repair advice. Production
authentication, licensed knowledge, booking and model/price credentials remain
external integrations. Video speech can be transcribed in the Gemini configuration;
the offline demo cannot transcribe audio.

## Quick start (native Python / existing database)

```bash
pip install -r requirements.txt -c requirements.lock.txt
```

```bash
cp .env.example .env   # set DATABASE_URL, keys, MEDIA_ENCRYPTION_KEY
```

```bash
python -m scripts.seed   # migrate, ingest sample manuals/products, print demo tokens
```

```bash
DEV_AUTH=true uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and click **Dev login**. API docs are at `/docs`.

**No API keys?** Set `CLOUD_PROVIDER=demo LOCAL_ENABLED=false`. That runs an offline stand-in that assembles answers from retrieved passages. It's useful for trying the whole workflow, but it does no real reasoning. For real use, set `OPENAI_API_KEY` or `GEMINI_API_KEY` and optionally run a local model through Ollama (`LOCAL_BASE_URL`, `LOCAL_MODEL`).

**Workers:** by default the job worker runs inside the API process. In production, set `RUN_WORKER_IN_PROCESS=false` and run workers separately. `runner.Worker` / `runner.run_pending` use `FOR UPDATE SKIP LOCKED`, so several workers can run at once.

## Tests

```bash
createdb fixbridge_test && psql fixbridge_test -c 'CREATE EXTENSION vector'
```

```bash
python -m pytest -q
```

The tests run against real Postgres with fake model adapters. They cover the end-to-end flows: DIY plan, clarification loop and its limit, safety warning continue/cancel, emergency short-circuit, cloud→local fallback, retry→failure, authorization, upload spoofing, encryption at rest, outcomes and eval export. Unit tests cover the safety rules, retrieval, ingestion and normalization.

## Manual crawler (official manufacturer sites)

```bash
python -m scripts.crawl_manuals ge --max-pdfs 50
```

`app/crawler.py` reads each manufacturer's sitemaps, follows product pages to manual PDFs on that manufacturer's own domains, and keeps owner's/use-and-care/installation/service/troubleshooting manuals from the last 15 years. It honours robots.txt (including Crawl-delay) and identifies itself as `FixBridgeManualBot`. Manuals are ingested as **pending** and are not used for answers until approved via `GET /api/admin/knowledge/pending` and `POST /api/admin/knowledge/review`. Each manufacturer needs a `Source` entry, and some sites block crawlers entirely (GE currently returns 403), so expect per-site work or licensing agreements.

## Live prices

With `PRICE_LOOKUP_PROVIDER=serpapi` and `SERPAPI_API_KEY`, parts without a fresh catalog price are looked up on Google Shopping through SerpAPI and cached for 24h. Customers can press **Get current prices** (`POST /api/repair-requests/{id}/refresh-prices`) to force a fresh lookup. Listings are marked "check fit" unless they name the customer's model.

## Integrating with the existing FixBridge site

- **Auth:** `app/security.py` issues and verifies HMAC tokens. Replace `current_user` with verification of your existing session or JWT.
- **UI:** either embed `app/static/` or call the JSON API. The response shape is fixed by `DiagnosisResponse` and its `outcome` field: `diy_plan | safety_warning | clarification | professional | cancelled | failure`.
- **Storage:** `LocalEncryptedStore` can be swapped for S3/GCS with the same `put`/`get` interface.
- **Booking:** the demo shows professional contact guidance; connect your booking service for production.

## Before production

- `sample_knowledge/` and the `SAMPLE-*` products are **illustrative content I wrote for testing**. Replace them with licensed manuals and a real price feed.
- Safety rules (`PROHIBITED`, `WARNINGS`, `HIGH_RISK_CATEGORIES`) are deliberately conservative. Have them reviewed by someone qualified for your jurisdictions.
- OCR for scanned PDFs needs `pytesseract`, `pypdfium2` + tesseract. Video audio transcription requires the configured Gemini provider.
- The rate limiter and circuit breakers are in-memory, i.e. per process. Move them to Redis when you scale out.

## Repeatable validation

Use a dedicated disposable database: the test fixture **drops its public schema**.
Set `TEST_DATABASE_URL` to override the default `fixbridge_test` database, then
run `python -m pytest -q`. GitHub Actions runs this suite with pgvector.
After starting the demo stack, run `python -m scripts.smoke_demo` from a Python
environment with the requirements installed. This signs in, submits a repair,
waits for the worker and verifies a cited result over HTTP.

For replacing Fixera inside the existing homeowner panel, see
[the integration contract](deploy/homeowner-panel.md). The standalone demo is
not the live website; panel source/session integration must be wired there.
