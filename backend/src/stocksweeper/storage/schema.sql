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

-- Issuances are immutable. A failed attempt is a row too, so coverage cannot
-- be inflated by scoring only forecasts that happened to be available.
CREATE TABLE IF NOT EXISTS forecast_issuances (
    idempotency_key VARCHAR PRIMARY KEY,
    contract_key VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    root VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    expiration DATE NOT NULL,
    expiry_session DATE NOT NULL,
    strike_exact VARCHAR NOT NULL,
    terms_note VARCHAR NOT NULL,
    contract_since DATE,
    input_session DATE,
    input_retrieved_at TIMESTAMPTZ,
    issued_at TIMESTAMPTZ NOT NULL,
    model_version VARCHAR,
    method VARCHAR,
    data_hash VARCHAR,
    distribution_hash VARCHAR,
    price_basis VARCHAR,
    spot_exact VARCHAR,
    status VARCHAR NOT NULL,
    itm_probability DOUBLE,
    otm_probability DOUBLE,
    atm_probability DOUBLE,
    unavailable_reason VARCHAR,
    provenance VARCHAR NOT NULL,
    volatility_regime VARCHAR,
    known_event_status VARCHAR,
    quote_time TIMESTAMPTZ,
    quote_source VARCHAR,
    quote_fetched_at TIMESTAMPTZ,
    quote_digest VARCHAR,
    snapshot_window VARCHAR,
    prepare_ms DOUBLE,
    lookup_ms DOUBLE
);

CREATE INDEX IF NOT EXISTS forecast_issuances_by_expiry
    ON forecast_issuances (expiry_session, ticker, input_session);

CREATE TABLE IF NOT EXISTS forecast_distributions (
    distribution_hash VARCHAR PRIMARY KEY,
    terminal_prices DOUBLE[] NOT NULL,
    weights DOUBLE[] NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL
);

-- Each source revision is a new row; issued probabilities are never updated.
CREATE TABLE IF NOT EXISTS forecast_labels (
    idempotency_key VARCHAR PRIMARY KEY,
    contract_key VARCHAR NOT NULL,
    terms_note VARCHAR NOT NULL,
    expiry_session DATE NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL,
    reason VARCHAR,
    source VARCHAR,
    nasdaq_close_exact VARCHAR,
    yahoo_close_exact VARCHAR,
    selected_close_exact VARCHAR,
    classification VARCHAR
);

CREATE INDEX IF NOT EXISTS forecast_labels_by_contract
    ON forecast_labels (contract_key, expiry_session, checked_at);

-- The fixed-cohort panel records empty selection cells as well as issued
-- contracts, preserving the denominator for prospective coverage checks.
CREATE TABLE IF NOT EXISTS forecast_panel_cells (
    sample_session DATE NOT NULL,
    ticker VARCHAR NOT NULL,
    cohort_hash VARCHAR NOT NULL,
    cohort_frozen_at TIMESTAMPTZ NOT NULL,
    input_session DATE NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL,
    horizon_band VARCHAR NOT NULL,
    moneyness VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    chain_source VARCHAR,
    chain_fetched_at TIMESTAMPTZ,
    selection_spot_exact VARCHAR,
    contract_key VARCHAR,
    forecast_status VARCHAR NOT NULL,
    reason VARCHAR,
    PRIMARY KEY (sample_session, ticker, horizon_band, moneyness, side)
);

CREATE TABLE IF NOT EXISTS market_curve_shadow_runs (
    ticker VARCHAR NOT NULL,
    chain_fetched_at TIMESTAMPTZ NOT NULL,
    source VARCHAR NOT NULL,
    session_date DATE NOT NULL,
    model_version VARCHAR NOT NULL,
    report_json JSON NOT NULL,
    PRIMARY KEY (ticker, chain_fetched_at, source, model_version)
);
