CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR PRIMARY KEY,
    kind VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    progress DOUBLE NOT NULL,
    message VARCHAR NOT NULL,
    error VARCHAR,
    run_id VARCHAR,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS watches (
    id VARCHAR PRIMARY KEY,
    watch_key VARCHAR NOT NULL UNIQUE,
    ticker VARCHAR NOT NULL,
    root VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    expiration DATE NOT NULL,
    strike DECIMAL(18, 3) NOT NULL,
    terms_note VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_attempted_session DATE,
    last_attempted_at TIMESTAMPTZ,
    last_error VARCHAR,
    outcome_warning VARCHAR
);

CREATE TABLE IF NOT EXISTS watch_outcomes (
    id VARCHAR PRIMARY KEY,
    watch_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    classification VARCHAR,
    reason VARCHAR,
    source VARCHAR,
    session_date DATE,
    retrieved_at TIMESTAMPTZ NOT NULL,
    close_price VARCHAR,
    terms_note VARCHAR NOT NULL,
    revised BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS watch_outcomes_by_watch
    ON watch_outcomes (watch_id, retrieved_at);
