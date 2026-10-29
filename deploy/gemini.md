# Gemini model integration

FixBridge uses Google's pretrained Gemini model with retrieved repair evidence.
No foundation model is trained here, and model weights do not belong in this Git
repository. `CLOUD_PROVIDER=gemini` selects the real provider; the demo adapter is
never an automatic fallback. All responses still pass through citation checking,
clarification limits, deterministic safety rules and plan validation.

## Configure credentials securely

Set `FIXBRIDGE_GEMINI_KEY` in your secret manager or environment settings. Existing
`GEMINI_API_KEY`, `GOOGLE_API_KEY` and `GEMINI_KEY` bindings are also supported.
Requests use the `x-goog-api-key` header over verified HTTPS to
`generativelanguage.googleapis.com`. Never put the key in a URL, source file or
browser. In the Codex environment, the secure key requirement and destination
have been saved in the draft; saving the draft alone does not inject a value.

Set `CLOUD_PROVIDER=gemini`, `CLOUD_ENABLED=true`, `LOCAL_ENABLED=false` and
`GEMINI_MODEL=gemini-flash-latest`. The default is Google's documented Flash alias;
responses record the actual `modelVersion`. For controlled production rollouts,
pin `GEMINI_MODEL` to a currently supported model ID after testing it in your
Google project. Quotas, model access, data policies and billing belong to that
project. The model receives relevant text and images; video speech is transcribed
before retrieval and emergency detection. Silence or unavailable transcription is
reported instead of fabricating observations. Set `TRANSCRIBE_AUDIO=false` to
explicitly disable audio submission. Only the first two minutes of video audio
are extracted; describe any later symptoms in text.

## Start the isolated Gemini stack

Install the Python requirements for the check commands. Copy
`.env.gemini.example` to `.env.gemini` and fill it securely. For the local stack,
`DATABASE_URL` must point to `db:5432/fixbridge`, with the password matching
`FIXBRIDGE_DB_PASSWORD`. Percent-encode password characters in the URL. Local
database traffic stays on the private Compose network; production database URLs
must use verified TLS. Generate a strong signing secret and a Fernet media key.

```bash
docker compose -p fixbridge-gemini --env-file .env.gemini -f compose.gemini.yml up --build -d --wait --wait-timeout 180
```

This uses a separate database and media volume from `compose.demo.yml` and binds
the API to loopback port 8002 by default. The seed step makes real embedding calls;
it will fail visibly if the key/network/quota is unusable. The in-process worker
handles diagnosis jobs. Enable `DEV_AUTH=true` only for a private local trial,
then use the seeded `demo@example.com` account. Do not publish those sample users
or illustrative repair manuals as a production knowledge base.

The Google embedding model is `gemini-embedding-001`, with separate retrieval
query/document task types and normalized 384-dimensional vectors. **Do not switch
an existing hash-embedding database to Gemini and reuse its vectors.** Rebuild the
knowledge index in a fresh database from approved original documents. Changing
the configured provider or rerunning seed does not re-embed unchanged documents.

## Verify real inference

With the key injected and `CLOUD_PROVIDER=gemini`, run from the repository root:

```bash
python -m scripts.check_ai
```

This makes real paid requests: structured diagnosis, reading a synthetic image
label, requesting clarification without evidence, a semantic query embedding,
and a silent-audio transcription check. It exits nonzero on missing credentials,
provider errors or failed assertions. It does not use a database or send customer
data. This is a small provider smoke check, not a repair-quality or safety
certification. Then, in a private local stack with dev auth enabled:

```bash
python -m scripts.smoke_demo http://127.0.0.1:8002 --require-provider gemini
```

Despite its filename, the HTTP check uses whichever provider the running service
is configured with. Inspect the result's provider/model and persisted model-call
records to confirm Gemini was used. `/health` checks process liveness;
`/health/ready` requires a database and a configured, non-circuit-broken provider.
Cloud readiness does not make a paid inference call; use `check_ai` for that.

## What is tested without credentials

`python -m pytest -q` exercises the real Gemini adapter through HTTP doubles and
the real PostgreSQL pipeline: schemas, photos, retries, blocked/truncated responses,
embedding dimensions, audio transcription, emergencies, citations and redaction.
Those tests prove application/provider-contract behavior, not live Google access
or model accuracy. Never report them as successful live inference.

The existing homeowner panel must mint service tokens or supply its real session
verifier; see `homeowner-panel.md`. Keep `DEV_AUTH=false` on the public website.
Use private S3 for multiple workers/tasks, share the encryption key, and enable
Gemini in every API/worker process. `aws/task-definition.json` shows Gemini as the
primary provider; fill deployment-specific identifiers before registering it.
