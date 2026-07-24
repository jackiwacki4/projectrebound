"""SQLite schema.

Design rules:
- All timestamps are stored as INTEGER Unix milliseconds, UTC. Integer columns
  compare cleanly, which is what the no-lookahead "as of" filter relies on.
- Observation tables are append-only. Nothing here ever UPDATEs a historical
  observation row. `markets` and `settlements` carry mutable status, but the
  raw observations (book snapshots, trades, forecasts, station obs, decisions)
  are immutable once written. Storage is cheap; lost history is unrecoverable.
"""

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Reference data about each market. Mutable status/result; not an observation.
CREATE TABLE IF NOT EXISTS markets (
    ticker        TEXT PRIMARY KEY,
    series        TEXT NOT NULL,
    city          TEXT,
    title         TEXT,
    close_ts      INTEGER,          -- market close (ms)
    status        TEXT,             -- open | closed | settled ...
    result        TEXT,             -- yes | no | NULL until settled
    first_seen_ts INTEGER NOT NULL,
    updated_ts    INTEGER NOT NULL
);

-- Append-only full-depth order-book snapshots.
CREATE TABLE IF NOT EXISTS book_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    captured_ts   INTEGER NOT NULL, -- when WE observed it (ms)
    yes_levels    TEXT NOT NULL,    -- JSON: [[price_cents, count], ...] best-first
    no_levels     TEXT NOT NULL,
    best_yes_bid  INTEGER,          -- cents; NULL if empty side
    best_yes_ask  INTEGER,
    best_no_bid   INTEGER,
    best_no_ask   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_book_ticker_ts ON book_snapshots(ticker, captured_ts);

-- Append-only observed trades.
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id      TEXT,             -- exchange trade id, for dedup (nullable)
    ticker        TEXT NOT NULL,
    trade_ts      INTEGER NOT NULL, -- exchange trade time (ms)
    captured_ts   INTEGER NOT NULL,
    price_cents   INTEGER NOT NULL,
    count         INTEGER NOT NULL,
    taker_side    TEXT              -- yes | no | NULL
);
CREATE INDEX IF NOT EXISTS ix_trades_ticker_ts ON trades(ticker, trade_ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_trade_id ON trades(trade_id)
    WHERE trade_id IS NOT NULL;

-- Append-only NWS forecasts, stamped with the time NWS ISSUED them so we can
-- reconstruct "what did we know at decision time" without lookahead.
CREATE TABLE IF NOT EXISTS forecasts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT NOT NULL DEFAULT 'nws', -- nws | open_meteo_hrrr | ... (ensemble member)
    station        TEXT NOT NULL,
    city           TEXT,
    issued_ts      INTEGER NOT NULL, -- when the source issued the forecast (ms)
    fetched_ts     INTEGER NOT NULL, -- when we fetched it (ms)
    target_date    TEXT NOT NULL,    -- YYYY-MM-DD local date the high is for
    forecast_high_f REAL,            -- predicted daily high, deg F
    raw            TEXT NOT NULL     -- JSON of the source payload
);
CREATE INDEX IF NOT EXISTS ix_forecasts_station_issued ON forecasts(station, issued_ts);
CREATE INDEX IF NOT EXISTS ix_forecasts_provider ON forecasts(provider, station, target_date);

-- Append-only station observations (used for model calibration / analysis).
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station       TEXT NOT NULL,
    obs_ts        INTEGER NOT NULL, -- observation time (ms)
    captured_ts   INTEGER NOT NULL,
    temp_c        REAL,
    raw           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_obs_station_ts ON observations(station, obs_ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_obs_station_obsts ON observations(station, obs_ts);

-- Append-only decision log: the probability, the inputs that produced it, and
-- the book state at decision time. Cannot be reconstructed after the fact.
CREATE TABLE IF NOT EXISTS decisions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_ts       INTEGER NOT NULL,
    ticker            TEXT NOT NULL,
    model_name        TEXT NOT NULL,
    probability       REAL NOT NULL,
    uncertainty       REAL,
    inputs            TEXT NOT NULL,   -- JSON structured inputs
    book_snapshot_id  INTEGER,
    best_yes_bid      INTEGER,
    best_yes_ask      INTEGER,
    intended_side     TEXT,            -- yes | no
    intended_price_cents INTEGER,
    edge_after_fees   REAL,
    gate_passed       INTEGER NOT NULL, -- 1 if all gates passed
    blocked_by        TEXT              -- gate name, or NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_ticker_ts ON decisions(ticker, decision_ts);

-- Append-only paper fills (hypothetical, full size, net of modelled fees).
CREATE TABLE IF NOT EXISTS paper_fills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id   INTEGER NOT NULL,
    ticker        TEXT NOT NULL,
    side          TEXT NOT NULL,    -- yes | no
    count         INTEGER NOT NULL,
    price_cents   INTEGER NOT NULL,
    fee_cents     INTEGER NOT NULL,
    filled_ts     INTEGER NOT NULL,
    settled_result TEXT,            -- filled once market resolves
    pnl_cents     INTEGER           -- net of fee, filled at settlement
);
CREATE INDEX IF NOT EXISTS ix_paper_ticker ON paper_fills(ticker);

-- Append-only micro-live orders (1 contract). status tracks the lifecycle.
CREATE TABLE IF NOT EXISTS live_orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id       INTEGER NOT NULL,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,
    count             INTEGER NOT NULL,     -- always 1 in Phase 1
    limit_price_cents INTEGER NOT NULL,
    client_order_id   TEXT NOT NULL,
    order_id          TEXT,
    status            TEXT NOT NULL,        -- submitted|filled|unfilled|cancelled|rejected
    fill_type         TEXT,                 -- maker|taker|none
    fill_price_cents  INTEGER,
    fee_cents         INTEGER,
    submitted_ts      INTEGER NOT NULL,
    resolved_ts       INTEGER,
    settled_result    TEXT,
    pnl_cents         INTEGER
);
CREATE INDEX IF NOT EXISTS ix_live_ticker ON live_orders(ticker);

-- Append-only settlement records (ground truth for P&L and calibration).
CREATE TABLE IF NOT EXISTS settlements (
    ticker        TEXT PRIMARY KEY,
    result        TEXT NOT NULL,    -- yes | no
    settled_ts    INTEGER,          -- exchange settlement time if known (ms)
    recorded_ts   INTEGER NOT NULL
);
"""
