-- FixBridge AI repair pipeline schema (PostgreSQL 16 + pgvector).
-- `{dim}` is replaced with EMBEDDING_DIM at migration time.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- identity
CREATE TABLE IF NOT EXISTS users (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email       text UNIQUE NOT NULL,
    name        text NOT NULL DEFAULT '',
    role        text NOT NULL DEFAULT 'customer' CHECK (role IN ('customer', 'reviewer', 'admin')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------- properties ("passport")
CREATE TABLE IF NOT EXISTS properties (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id      uuid NOT NULL REFERENCES users(id),
    name          text NOT NULL,
    address       text NOT NULL DEFAULT '',
    property_type text NOT NULL DEFAULT 'house',
    year_built    int,
    passport      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- known facts (utilities, materials, notes)
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS property_members (
    property_id uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (property_id, user_id)
);

CREATE TABLE IF NOT EXISTS equipment (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id      uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    service_category text NOT NULL,
    kind             text NOT NULL,           -- e.g. "water heater", "dishwasher"
    brand            text,
    model_number     text,
    serial_number    text,
    install_year     int,
    notes            text NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS service_history (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    request_id  uuid,
    event_type  text NOT NULL,                -- diagnosis | repair | professional_visit | note
    summary     text NOT NULL,
    details     jsonb NOT NULL DEFAULT '{}'::jsonb,
    source      text NOT NULL DEFAULT 'user_reported' CHECK (source IN ('user_reported', 'verified', 'system')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------- repair requests
CREATE TABLE IF NOT EXISTS repair_requests (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id         uuid NOT NULL REFERENCES properties(id),
    user_id             uuid NOT NULL REFERENCES users(id),
    service_category    text NOT NULL,
    description         text NOT NULL,
    equipment_id        uuid REFERENCES equipment(id),
    status              text NOT NULL DEFAULT 'received',
    stage               text NOT NULL DEFAULT 'received',
    clarification_round int NOT NULL DEFAULT 0,
    idempotency_key     text,
    error               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS media (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id     uuid NOT NULL REFERENCES repair_requests(id) ON DELETE CASCADE,
    kind           text NOT NULL CHECK (kind IN ('image', 'video', 'document')),
    mime_type      text NOT NULL,
    filename       text NOT NULL,
    size_bytes     bigint NOT NULL,
    sha256         text NOT NULL,
    storage_key    text NOT NULL,
    encrypted      boolean NOT NULL DEFAULT true,
    extracted_text text,
    derived        jsonb NOT NULL DEFAULT '{}'::jsonb,   -- thumbnails, frames, audio, dims
    clarification_round int NOT NULL DEFAULT 0,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id          bigserial PRIMARY KEY,
    request_id  uuid NOT NULL REFERENCES repair_requests(id) ON DELETE CASCADE,
    kind        text NOT NULL DEFAULT 'diagnose',
    status      text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts    int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    run_after   timestamptz NOT NULL DEFAULT now(),
    last_error  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_queue_idx ON jobs (status, run_after);

-- ------------------------------------------------------ knowledge base
CREATE TABLE IF NOT EXISTS knowledge_documents (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title            text NOT NULL,
    source           text NOT NULL,          -- URL / publisher reference
    publisher        text NOT NULL DEFAULT '',
    permission       text NOT NULL CHECK (permission IN ('approved', 'licensed', 'public_domain', 'internal')),
    approved_by      text NOT NULL,
    service_category text NOT NULL,
    brand            text,
    model_numbers    text[] NOT NULL DEFAULT '{}',
    version          text NOT NULL,
    checksum         text NOT NULL,
    active           boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, version)
);

CREATE TABLE IF NOT EXISTS knowledge_passages (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id      uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    ord              int NOT NULL,
    page             int,
    section          text NOT NULL DEFAULT '',
    content          text NOT NULL,
    is_table         boolean NOT NULL DEFAULT false,
    service_category text NOT NULL,
    brand            text,
    model_numbers    text[] NOT NULL DEFAULT '{}',
    version          text NOT NULL,
    tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', section || ' ' || content)) STORED,
    embedding        vector({dim}) NOT NULL
);
CREATE INDEX IF NOT EXISTS passages_tsv_idx ON knowledge_passages USING gin (tsv);
CREATE INDEX IF NOT EXISTS passages_models_idx ON knowledge_passages USING gin (model_numbers);
CREATE INDEX IF NOT EXISTS passages_embedding_idx ON knowledge_passages USING hnsw (embedding vector_cosine_ops);

-- ------------------------------------------------- products & prices
CREATE TABLE IF NOT EXISTS products (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sku                 text UNIQUE NOT NULL,
    name                text NOT NULL,
    brand               text,
    service_category    text NOT NULL,
    part_type           text NOT NULL,       -- normalized key, e.g. "anode_rod"
    compatible_models   text[] NOT NULL DEFAULT '{}',   -- empty = universal
    price_cents         int NOT NULL,
    currency            text NOT NULL DEFAULT 'USD',
    price_source        text NOT NULL,
    price_verified_at   timestamptz NOT NULL
);

-- ------------------------------------------------- AI results & safety
CREATE TABLE IF NOT EXISTS diagnoses (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id  uuid NOT NULL REFERENCES repair_requests(id) ON DELETE CASCADE,
    version     int NOT NULL,
    outcome     text NOT NULL,      -- diy_plan | safety_warning | clarification | professional | failure
    provider    text,
    model       text,
    diagnosis   jsonb,
    evidence    jsonb NOT NULL DEFAULT '[]'::jsonb,
    safety      jsonb NOT NULL DEFAULT '{}'::jsonb,
    repair_plan jsonb,
    response    jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (request_id, version)
);

CREATE TABLE IF NOT EXISTS clarifications (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id  uuid NOT NULL REFERENCES repair_requests(id) ON DELETE CASCADE,
    round       int NOT NULL,
    questions   jsonb NOT NULL,
    answers     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    answered_at timestamptz,
    UNIQUE (request_id, round)
);

CREATE TABLE IF NOT EXISTS safety_acknowledgements (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id        uuid NOT NULL REFERENCES repair_requests(id) ON DELETE CASCADE,
    diagnosis_version int NOT NULL,
    user_id           uuid NOT NULL REFERENCES users(id),
    choice            text NOT NULL CHECK (choice IN ('continue', 'cancel')),
    warning_codes     text[] NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------- outcomes & feedback
CREATE TABLE IF NOT EXISTS outcomes (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id        uuid NOT NULL REFERENCES repair_requests(id) ON DELETE CASCADE,
    user_id           uuid NOT NULL REFERENCES users(id),
    resolution        text NOT NULL CHECK (resolution IN ('fixed_diy', 'fixed_professional', 'not_fixed', 'abandoned')),
    parts_used        jsonb NOT NULL DEFAULT '[]'::jsonb,
    actual_cost_cents int,
    rating            int CHECK (rating BETWEEN 1 AND 5),
    feedback          text NOT NULL DEFAULT '',
    label             text NOT NULL DEFAULT 'user_reported' CHECK (label IN ('user_reported', 'verified')),
    verified_by       uuid REFERENCES users(id),
    approved_for_eval boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------- observability
CREATE TABLE IF NOT EXISTS model_calls (
    id            bigserial PRIMARY KEY,
    request_id    uuid REFERENCES repair_requests(id) ON DELETE SET NULL,
    provider      text NOT NULL,
    model         text NOT NULL,
    success       boolean NOT NULL,
    latency_ms    int NOT NULL,
    input_tokens  int,
    output_tokens int,
    cost_usd      numeric(12, 6),
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         bigserial PRIMARY KEY,
    actor_id   uuid,
    action     text NOT NULL,
    target     text,
    details    jsonb NOT NULL DEFAULT '{}'::jsonb,
    ip         text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
    id         bigserial PRIMARY KEY,
    severity   text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    kind       text NOT NULL,
    message    text NOT NULL,
    details    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------- v2: crawled manuals + review workflow
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS review_status text NOT NULL DEFAULT 'approved';
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS source_url text;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS doc_type text;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS published_year int;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS raw_storage_key text;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS reviewed_by text;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;
ALTER TABLE knowledge_documents DROP CONSTRAINT IF EXISTS knowledge_documents_permission_check;
ALTER TABLE knowledge_documents ADD CONSTRAINT knowledge_documents_permission_check
    CHECK (permission IN ('approved', 'licensed', 'public_domain', 'internal', 'manufacturer_public'));
ALTER TABLE knowledge_documents DROP CONSTRAINT IF EXISTS knowledge_documents_review_status_check;
ALTER TABLE knowledge_documents ADD CONSTRAINT knowledge_documents_review_status_check
    CHECK (review_status IN ('pending', 'approved', 'rejected'));
CREATE INDEX IF NOT EXISTS knowledge_documents_checksum_idx ON knowledge_documents (checksum);
CREATE INDEX IF NOT EXISTS knowledge_documents_review_idx ON knowledge_documents (review_status, brand);

-- Resumable crawl frontier. One row per URL; workers claim with SKIP LOCKED.
CREATE TABLE IF NOT EXISTS crawl_urls (
    url            text PRIMARY KEY,
    source         text NOT NULL,                 -- crawler source name, e.g. "ge"
    kind           text NOT NULL CHECK (kind IN ('sitemap', 'page', 'pdf')),
    status         text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'running', 'done', 'skipped', 'failed')),
    attempts       int NOT NULL DEFAULT 0,
    http_status    int,
    discovered_from text,
    context        jsonb NOT NULL DEFAULT '{}'::jsonb,   -- e.g. model number / title from the product page
    result         text,
    fetched_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS crawl_urls_queue_idx ON crawl_urls (source, status, kind);

-- Live price lookups (e.g. Google Shopping via SerpAPI), cached per query.
CREATE TABLE IF NOT EXISTS price_lookups (
    id          bigserial PRIMARY KEY,
    query       text NOT NULL,
    provider    text NOT NULL,
    results     jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS price_lookups_query_idx ON price_lookups (query, created_at DESC);
