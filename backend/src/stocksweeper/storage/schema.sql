DROP VIEW IF EXISTS market;

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

CREATE TABLE IF NOT EXISTS runs (
    id VARCHAR PRIMARY KEY,
    created_at TIMESTAMP,
    config_json VARCHAR,
    status VARCHAR,
    strategy_count INTEGER,
    ticker_count INTEGER
);

CREATE TABLE IF NOT EXISTS strategies (
    id VARCHAR PRIMARY KEY,
    name VARCHAR,
    family VARCHAR,
    definition_json VARCHAR,
    signals VARCHAR
);

CREATE TABLE IF NOT EXISTS results (
    run_id VARCHAR,
    strategy_id VARCHAR,
    ticker VARCHAR,
    segment VARCHAR,
    cagr DOUBLE,
    total_return DOUBLE,
    sharpe DOUBLE,
    sortino DOUBLE,
    max_drawdown DOUBLE,
    calmar DOUBLE,
    win_rate DOUBLE,
    profit_factor DOUBLE,
    avg_trade DOUBLE,
    median_trade DOUBLE,
    n_trades INTEGER,
    exposure DOUBLE,
    avg_holding_period DOUBLE,
    PRIMARY KEY (run_id, strategy_id, ticker, segment)
);

CREATE TABLE IF NOT EXISTS robustness (
    run_id VARCHAR,
    strategy_id VARCHAR,
    ticker VARCHAR,
    score DOUBLE,
    sharpe_component DOUBLE,
    cagr_component DOUBLE,
    drawdown_component DOUBLE,
    profit_factor_component DOUBLE,
    trade_component DOUBLE,
    walk_forward_component DOUBLE,
    stability_component DOUBLE,
    degradation DOUBLE,
    flags_json VARCHAR,
    rejected BOOLEAN,
    rank INTEGER,
    PRIMARY KEY (run_id, strategy_id, ticker)
);

CREATE TABLE IF NOT EXISTS walk_forward (
    run_id VARCHAR,
    strategy_id VARCHAR,
    ticker VARCHAR,
    fold INTEGER,
    is_sharpe DOUBLE,
    oos_sharpe DOUBLE,
    oos_return DOUBLE,
    PRIMARY KEY (run_id, strategy_id, ticker, fold)
);

CREATE TABLE IF NOT EXISTS walk_forward_reopt (
    run_id VARCHAR,
    ticker VARCHAR,
    family VARCHAR,
    fold INTEGER,
    strategy_id VARCHAR,
    oos_sharpe DOUBLE,
    oos_return DOUBLE,
    PRIMARY KEY (run_id, ticker, family, fold)
);

CREATE TABLE IF NOT EXISTS cross_ticker (
    run_id VARCHAR,
    strategy_id VARCHAR,
    cross_score DOUBLE,
    per_ticker_json VARCHAR,
    qualified BOOLEAN,
    PRIMARY KEY (run_id, strategy_id)
);

CREATE TABLE IF NOT EXISTS run_tickers (
    run_id VARCHAR,
    ticker VARCHAR,
    n_bars INTEGER,
    first_ts DATE,
    last_ts DATE,
    bars_hash VARCHAR,
    limited_history BOOLEAN,
    survivors INTEGER,
    PRIMARY KEY (run_id, ticker)
);

CREATE TABLE IF NOT EXISTS benchmarks (
    run_id VARCHAR,
    ticker VARCHAR,
    segment VARCHAR,
    cagr DOUBLE,
    total_return DOUBLE,
    sharpe DOUBLE,
    sortino DOUBLE,
    max_drawdown DOUBLE,
    calmar DOUBLE,
    PRIMARY KEY (run_id, ticker, segment)
);
