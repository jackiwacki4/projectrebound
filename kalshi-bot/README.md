# projectrebound — Phase 1 (Kalshi validation harness)

**This is not a profit-seeking bot.** It is an instrumentation and validation
harness whose one job is to find out whether a *predictive edge exists at all*,
with the machinery for real trading already in place but deliberately
constrained. It collects market + weather data, runs an inspectable prediction
model, and routes every candidate trade down two paths — a full-size **paper**
record and a **1-contract micro-live** order — so the gap between them measures
the real cost (slippage + adverse selection) that paper trading hides.

It is a separate Python project living alongside the original TypeScript
arbitrage bot in this repo; the two share nothing.

## The non-negotiable safety rules (built in, tested)

1. **It never moves money.** No deposits, no withdrawals, no transfers — ever.
   A denylist guard in the HTTP layer (`clients/http_guard.py`) hard-fails any
   request whose path looks like funding/transfer/banking, and a test asserts
   every such path is blocked. Adding money to the account is something only you
   do, by hand, in Kalshi's own app.
2. **Live orders are 1 contract, max.** The ceiling is a hard-coded constant in
   `execution/live_engine.py` (`PHASE1_CONTRACT_LIMIT = 1`) — it is *not* a
   config value and cannot be raised by editing YAML.
3. **Live trading is off by default** and gated twice: `live_trading.enabled`
   must be set to `true` by hand in the config, and every risk gate must pass
   for each order.
4. **Credentials never touch the code or the logs.** They come from a gitignored
   `.env`; the private key is referenced by path; a log redaction filter strips
   anything key-shaped even at debug level.

## What it does each cycle

1. Snapshots the full order book (not just last trade) for the configured
   markets, plus observed trades, and stores them append-only in SQLite.
2. Collects free NWS forecasts, stamped with the time NWS *issued* them.
3. Runs the weather model as-of "right now" — and the data layer physically
   cannot hand the model any record newer than that instant, so lookahead bias
   is structurally impossible, not just discouraged.
4. Compares the model's probability to the order-book price, nets out the exact
   Kalshi fee, and if there's enough edge, records a paper fill and (only if
   live is enabled and all gates pass) places one real 1-contract order.
5. Records settlement outcomes and, on demand, prints a calibration report.

First market family: **daily high-temperature markets**. They resolve every
day (fast sample growth), settle on a public mechanical source, and their
predictive inputs (NWS forecasts) are free. A second family (sports) can be
added as a module later — the market-family seam is already there — but is out
of scope for Phase 1.

## Requirements

- macOS (Apple Silicon fine), Python 3.11+.
- `pip install -r requirements.txt` (cryptography, PyYAML, websockets).
- A Kalshi API key (create one at kalshi.com/account/profile → API Keys; the
  private key PEM is shown once — save it).

## Setup

```sh
cd kalshi-bot
python3 -m pip install -r requirements.txt

python3 -m kalshibot.cli init            # writes config/config.yaml from the example
cp .env.example .env                      # then edit .env with your key id + key path
# edit config/config.yaml: pick cities, confirm each NWS station, set risk limits
```

Run it (data collection + paper decisions; no live orders unless you enable them):

```sh
PYTHONPATH=src python3 -m kalshibot.cli run
```

Other commands:

```sh
PYTHONPATH=src python3 -m kalshibot.cli report          # the validation report
PYTHONPATH=src python3 -m kalshibot.cli health          # JSON health snapshot
PYTHONPATH=src python3 -m kalshibot.cli reset-breaker   # clear a tripped loss breaker (manual)
```

To stop live orders instantly without touching the process: `touch ./HALT`
(the kill-switch file; delete it to resume). Data collection keeps running.

## The intended order of operations

Follow the build's own logic: **collect data first, decide later.**

1. Run in the default (paper, live-off) mode and let it accumulate resolved
   markets for a while. `report` will tell you at the top when the sample is
   still too small to conclude anything (below ~100 resolved markets).
2. Read the calibration report. If the model shows no edge over enough
   resolved markets, that is the answer — and finding it out without risking
   money is the entire point. Live trading would be wasted work.
3. Only if there's a real, calibrated edge, consider enabling micro-live
   (below) to measure how much of that edge survives real fills.

## Running 24/7 on a MacBook

A `launchd` agent is provided at
`launchd/com.projectrebound.kalshibot.plist` — it auto-starts on login and
restarts on crash. Edit the paths inside it, then:

```sh
cp launchd/com.projectrebound.kalshibot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.projectrebound.kalshibot.plist
launchctl start com.projectrebound.kalshibot
```

The plist wraps the process in `caffeinate -s`, which keeps the Mac awake while
**on power** so sleep doesn't interrupt collection. Tradeoff: on battery this
would drain it and add heat, so `-s` only holds the system awake while plugged
in. The process also detects sleep/wake gaps and network drops itself: after
any gap it marks all cached state **stale** and refuses to place live orders
until a fresh resync completes — collection resumes automatically.

Logs are structured JSON in `logs/`, size-capped and rotated so they can't fill
the disk over months.

## Before you EVER enable live trading

Live trading places real orders. Even at 1 contract, do this first:

1. Set `KALSHI_ENVIRONMENT=demo` in `.env` and run with `live_trading.enabled:
   true` against Kalshi's sandbox. Confirm orders behave as expected.
2. **Verify the order-placement body.** `execution/live_engine.py::submit`
   builds the order from Kalshi docs that disagreed across sources when this was
   written (the same caveat as the TypeScript sibling project). Diff it against
   the current https://docs.kalshi.com/api-reference/orders/create-order-v2
   before trusting it with real money.
3. **Re-verify the fee schedule.** `execution/fees.py` implements the published
   formula (taker `0.07·C·P·(1−P)`, rounded up to the next cent; maker 25% of
   that; no settlement/membership fee). The live PDF rate-limited automated
   fetches when this was written — confirm the coefficients against
   kalshi.com/docs/kalshi-fee-schedule.pdf.
4. Only then switch `KALSHI_ENVIRONMENT=prod`.

## Tests

```sh
python3 -m pytest        # from the kalshi-bot/ directory
```

The safety-critical modules (fees, risk gates, storage, no-lookahead) depend
only on the standard library, so those tests run with nothing installed.
Coverage includes: the exact fee math at every boundary, the funding denylist
(and that the client routes through it), the structural no-lookahead guarantee,
every risk gate (never mocked away), settlement/P&L math, circuit-breaker
persistence + manual-only reset, and a full-pipeline integration test with live
execution disabled.

## Out of scope for Phase 1 (deliberately)

Deposits/withdrawals (permanently), sports/crypto/election markets, any size
above 1 contract, Kelly/bankroll sizing, cross-venue arbitrage, machine-learned
models (the weather model is explicit so its mistakes are legible), and any web
UI or dashboard — this phase is CLI only.
