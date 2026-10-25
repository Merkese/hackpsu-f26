-- Schema for the evidence pipeline. Safe to re-run: every statement is idempotent.
-- Apply with: python db.py schema
-- Statements are split on semicolons by db.py (a continuous aggregate cannot be
-- created inside a transaction block), so keep semicolons out of comments.
-- Not touched here: the pre-existing events, events_hourly and doc_chunks objects.

CREATE TABLE IF NOT EXISTS papers (
  id            serial PRIMARY KEY,
  pmcid         text UNIQUE NOT NULL,
  doi           text,
  title         text,
  year          int,
  license       text,
  open_license  boolean DEFAULT false
);

CREATE TABLE IF NOT EXISTS runs (
  id                 serial PRIMARY KEY,
  topic              text NOT NULL,
  research_question  text,
  hit_count          int,
  is_cached          boolean DEFAULT false,
  started_at         timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS run_papers (
  run_id            int REFERENCES runs(id),
  paper_id          int REFERENCES papers(id),
  stage             text NOT NULL
    CHECK (stage IN ('searched', 'screened', 'fetched', 'extracted', 'checked', 'excluded')),
  reason            text,
  relevance         text,
  relevance_reason  text,
  relevance_quote   text,
  screen_status     text,
  PRIMARY KEY (run_id, paper_id)
);

ALTER TABLE runs ADD COLUMN IF NOT EXISTS research_question text;
ALTER TABLE run_papers ADD COLUMN IF NOT EXISTS relevance text;
ALTER TABLE run_papers ADD COLUMN IF NOT EXISTS relevance_reason text;
ALTER TABLE run_papers ADD COLUMN IF NOT EXISTS relevance_quote text;
ALTER TABLE run_papers ADD COLUMN IF NOT EXISTS screen_status text;

CREATE TABLE IF NOT EXISTS extractions (
  paper_id        int REFERENCES papers(id),
  source          text CHECK (source IN ('abstract', 'body')),
  data            jsonb NOT NULL,
  model           text,
  prompt_version  text,
  created_at      timestamptz DEFAULT now(),
  PRIMARY KEY (paper_id, source)
);

CREATE TABLE IF NOT EXISTS checks (
  paper_id  int REFERENCES papers(id),
  name      text,
  status    text CHECK (status IN ('pass', 'partial', 'flag', 'unclear')),
  detail    text,
  PRIMARY KEY (paper_id, name)
);

-- Stage events for the live dashboard. No pipeline_events existed in Tiger Data,
-- so it is created here as a hypertable on ts.
CREATE TABLE IF NOT EXISTS pipeline_events (
  ts        timestamptz NOT NULL DEFAULT now(),
  run_id    int,
  paper_id  int,
  stage     text NOT NULL
    CHECK (stage IN ('searched', 'screened', 'fetched', 'extracted', 'checked', 'excluded')),
  status    text NOT NULL DEFAULT 'ok'
    CHECK (status IN ('ok', 'fail')),
  detail    text
);

SELECT create_hypertable('pipeline_events', 'ts', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS pipeline_events_run_ts ON pipeline_events (run_id, ts DESC);

-- One-minute stage counts, including rows not yet materialized.
CREATE MATERIALIZED VIEW IF NOT EXISTS stage_stats_1m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 minute', ts) AS bucket,
       run_id,
       stage,
       status,
       count(*) AS n
FROM pipeline_events
GROUP BY bucket, run_id, stage, status
WITH NO DATA;
