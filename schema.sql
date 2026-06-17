-- catalogger schema
-- Run with: catalogger initdb   (or psql -f schema.sql)

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- bodies: content-addressed, deduplicated response/request bodies.
-- One row per UNIQUE body. `content` is zstd-compressed; sha256 is of the
-- ORIGINAL (uncompressed) bytes, so dedup keys on plaintext content.
-- seen_count tells you how many times this exact body has been observed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bodies (
    sha256      char(64)    PRIMARY KEY,
    size        integer     NOT NULL,          -- original uncompressed size
    encoding    text        NOT NULL DEFAULT 'zstd',
    content     bytea       NOT NULL,          -- compressed body bytes
    is_text     boolean     NOT NULL DEFAULT false,
    seen_count  bigint      NOT NULL DEFAULT 1,
    first_seen  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- body_text: full-text index over UNIQUE text bodies only.
-- Because bodies are deduplicated, this FTS corpus is tiny relative to total
-- traffic -- the millionth identical 404 contributes nothing here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS body_text (
    sha256  char(64) PRIMARY KEY REFERENCES bodies(sha256) ON DELETE CASCADE,
    tsv     tsvector NOT NULL
);
CREATE INDEX IF NOT EXISTS body_text_tsv_idx ON body_text USING gin (tsv);

-- ---------------------------------------------------------------------------
-- flows: full-fidelity record of every captured request/response.
-- Bodies are referenced by hash (the dedup pointer), never inlined.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS flows (
    id            bigserial   PRIMARY KEY,
    ts            timestamptz NOT NULL,
    program       text,
    source_tool   text,
    session_id    text,                        -- Claude Code session correlation
    method        text        NOT NULL,
    scheme        text        NOT NULL,
    host          text        NOT NULL,
    port          integer,
    path          text        NOT NULL,
    query         text,
    url           text        NOT NULL,
    status        integer,
    req_headers   jsonb,
    resp_headers  jsonb,
    req_body_sha  char(64)    REFERENCES bodies(sha256),
    resp_body_sha char(64)    REFERENCES bodies(sha256),
    duration_ms   integer,
    fingerprints  text[]      NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS flows_host_idx         ON flows (host);
CREATE INDEX IF NOT EXISTS flows_ts_idx           ON flows (ts);
CREATE INDEX IF NOT EXISTS flows_status_idx       ON flows (status);
CREATE INDEX IF NOT EXISTS flows_session_idx      ON flows (session_id);
CREATE INDEX IF NOT EXISTS flows_program_idx      ON flows (program);
CREATE INDEX IF NOT EXISTS flows_resp_sha_idx     ON flows (resp_body_sha);
CREATE INDEX IF NOT EXISTS flows_fingerprints_idx ON flows USING gin (fingerprints);
CREATE INDEX IF NOT EXISTS flows_host_trgm_idx    ON flows USING gin (host gin_trgm_ops);
CREATE INDEX IF NOT EXISTS flows_url_trgm_idx     ON flows USING gin (url gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- flow_agg: collapsed aggregate for high-volume fuzz streams.
-- Instead of one flow row per request, one row per distinct
-- (host, method, path, status, body) shape with a hit counter.
-- shape_sha is computed app-side so a nullable body can be part of the key.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS flow_agg (
    shape_sha     char(64)    PRIMARY KEY,
    program       text,
    source_tool   text,
    method        text        NOT NULL,
    host          text        NOT NULL,
    path          text        NOT NULL,
    status        integer,
    resp_body_sha char(64)    REFERENCES bodies(sha256),
    hit_count     bigint      NOT NULL DEFAULT 1,
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    fingerprints  text[]      NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS flow_agg_host_idx ON flow_agg (host);
CREATE INDEX IF NOT EXISTS flow_agg_fp_idx   ON flow_agg USING gin (fingerprints);
