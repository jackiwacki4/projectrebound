# projectrebound — Phase 1 (Kalshi validation harness)

**This is not a profit-seeking bot.** It is an instrumentation and validation
harness whose one job is to find out whether a *predictive edge exists at all*,
with the machinery for real trading already in place but deliberately
constrained. It collects market data plus the inputs a model needs, runs an
inspectable prediction model, and routes every candidate trade down two paths —
a full-size **paper** record and a **1-contract micro-live** order — so the gap
between them measures the real cost (slippage + adverse selection) that paper
trading hides.

Two market families are implemented, chosen by `market_family` in the config:
**weather** (daily high-temperature markets) and **sports** (game-winner
markets). They are the same machine pointed at a different driver — see
[The sports family](#the-sports-family).

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
2. Collects the running family's model inputs from **multiple independent
   sources** — weather forecasts, or sports rating methods — plus a live feed of
   what has actually happened so far (airport observations, or the game score),
   each stamped with when it was known.
3. Runs the ensemble model as-of "right now" — and the data layer physically
   cannot hand the model any record newer than that instant, so lookahead bias is
   structurally impossible, not just discouraged.
4. Compares the model's probability to the order-book price, nets out the exact
   Kalshi fee, and if there's enough edge, records a paper fill and (only if
   live is enabled and all gates pass) places one real 1-contract order.
5. Records settlement outcomes and, on demand, prints a report: calibration,
   **accuracy bucketed by how long before settlement each call was made** (does
   it get sharper late in the day?), and a **per-trade ledger** — every entry,
   whether it settled as a win ($1.00) or loss ($0.00), and the P&L both at
   1 contract and scaled to a dollar stake you choose (`--stake`, default $10).
   The scaled figure is a hypothetical: it assumes the whole size filled at the
   displayed price, which thin books won't honor — the 1-contract live column is
   the reality check.

## Data sources (the weather ensemble)

The model doesn't trust one forecast — it combines several, and treats their
*disagreement* as its own uncertainty (when the models diverge, it bets less
confidently). Providers, all free and quick, behind one interface in
`clients/forecast_providers.py`:

| Provider | Source | Role |
|---|---|---|
| `nws` | NOAA NWS gridpoint forecast (api.weather.gov) | forecast member |
| `open_meteo_best` | Open-Meteo auto blend | forecast member |
| `open_meteo_hrrr` | NOAA **HRRR** (high-res short-range) | forecast member |
| `open_meteo_gfs` | NOAA **GFS** (global) | forecast member |
| `open_meteo_ecmwf` | **ECMWF IFS** open data | forecast member |
| `open_meteo_ecmwf_ai` | ECMWF **AIFS** (their AI model) — off by default | forecast member |
| `metar` | airport **METAR** observations (aviationweather.gov) | observation clamp |

The METAR feed is what has *actually happened* so far today — the model uses it
to clamp its answer, since the day's high can only be at or above what's already
been observed. Enable/disable providers in `config.yaml` under `weather`.

**A note on HRRR / GFS / ECMWF:** these come through Open-Meteo, which ingests
those exact model runs and serves them as clean JSON. That's the "accurate and
quick" path — it gets all three models without downloading multi-hundred-MB
GRIB files or installing GRIB decoders. If you ever want to pull the *raw* GRIB
straight from NOAA NOMADS and ECMWF Open Data (fresher by minutes, but much
heavier), that can be added as another provider behind the same interface —
ask and it's a bounded add.

First market family: **daily high-temperature markets**. They resolve every
day (fast sample growth), settle on a public mechanical source, and their
predictive inputs (NWS forecasts) are free.

## The sports family

Sports game-winner markets ("Will Seattle win?"), built on the *same model* as
the weather one rather than a new idea. The weather model treats the settlement
temperature as Normally distributed around an ensemble of forecast highs and
integrates over the market's threshold. The sports model does exactly that with a
different driver — the **game margin** (home score minus away score):

| | weather | sports |
|---|---|---|
| driver | daily high (°F) | final margin (runs / points) |
| ensemble member | one NWP model's forecast high | one rating method's expected margin |
| base sigma | forecast error | irreducible game randomness |
| spread | model disagreement | method disagreement |
| observation | METAR temperature so far today | live score + inning so far |
| threshold | "97° or above" | "margin ≥ 1", i.e. a win |

Members, behind one interface in `clients/sports_providers.py`:

| Provider | What it reads | Role |
|---|---|---|
| `elo` | Elo replayed over the completed games the bot has collected | recent form |
| `log5` | Bradley-Terry / log5 on season win-loss records | season-long strength |
| `pythagorean` | expected win rate from runs/points scored and allowed | strength net of luck |
| `espn_bookmaker` | DraftKings line via ESPN — **off by default** | market comparison |
| `espn_scoreboard` | live score, inning/quarter, final status | observation clamp |

All of it comes from ESPN's public endpoints — free, no key, no registration
(`clients/espn_client.py`). Nothing new needs to be signed up for.

**The live-score clamp** is the same statement as the weather model's "the day's
high can only be at or above what's already been observed": the final margin is
the current lead *plus* whatever happens in the innings left, so as a game runs
out the lead becomes decisive and the remaining uncertainty shrinks with the
square root of the time remaining. A 3-run lead in the 2nd and the same lead in
the 8th are priced very differently, and once the game is final the answer is 0
or 1. This is also why the model **refuses to have an opinion** on a game that has
started when the score feed is stale (`sports.max_state_age_seconds`) — an
in-progress game with a dead feed is exactly where a pre-game probability is most
confidently wrong.

**Two honest caveats**, both of which are the reason for a config knob rather
than a footnote:

1. **The members are not independent.** The weather ensemble combines genuinely
   different physical models, so their disagreement is a fair proxy for
   uncertainty. Elo, log5 and pythagorean all read the same season of the same
   games from the same feed: they agree far more than they are jointly right, so
   their spread *understates* real uncertainty. `model.min_sigma_floor` puts a
   floor under the ensemble's sigma so a false consensus cannot be priced as
   confidence.
2. **The bookmaker member is off by default.** A sportsbook line is not a
   prediction method, it is another market's price. Switching it on changes the
   question from "can an independent model find mispricing?" to "does Kalshi
   disagree with DraftKings?" — a legitimate and probably more profitable
   question, but a different one, and it would dominate the ensemble. Turn it on
   deliberately, knowing which experiment you are now running.

To run the sports family, set `market_family: sports` in `config.yaml` (the
example file ships with an MLB league block filled in and NFL commented out) and
point `storage.db_path` at a separate database if you also run the weather one —
one family per process.

Two things to know about the first day or two of sports collection:

- **Elo starts cold.** It replays only the results the bot has collected
  (`sports.results_lookback_days`), starting everyone at 1500, so on the very
  first run it abstains until each team has `elo_min_games` games in the history.
  The record-based members work immediately.
- **Every market must be linked to a real game first.** Kalshi's ticker
  (`KXMLBGAME-26JUL282210SEALAD-SEA`) names the teams but not which is at home, so
  each market is matched to an ESPN game before the model will price it. If a
  league logs repeated `no ESPN game matched` warnings, its team codes disagree
  with ESPN's and need adding to the per-league table in `clients/espn_client.py`
  (verified there for MLB and NFL).

## Requirements

- macOS (Apple Silicon fine), Python 3.11+.
- `pip install -r requirements.txt` (cryptography, PyYAML, websockets).
- A Kalshi API key (create one at kalshi.com/account/profile → API Keys; the
  private key PEM is shown once — save it).

## Setup

```sh
cd kalshi-bot
./setup.sh
```

That one command checks your Python version, installs everything into a private
`.venv` (which also avoids the usual macOS "externally managed environment" pip
error), and creates `config/config.yaml` and `.env`. It never overwrites files
you've already edited, so it's safe to re-run.

Then the two things only you can do:

1. Create a Kalshi API key (kalshi.com → Account → Profile → API Keys) and save
   the downloaded private-key file as `secrets/kalshi_private_key.pem`.
2. `open -e .env` and set `KALSHI_API_KEY_ID` to your Key ID.

Run it (data collection + paper decisions; no live orders unless you enable them):

```sh
./run.sh run
```

`run.sh` is a thin wrapper so you never think about `PYTHONPATH` or virtualenvs.
If credentials are missing it tells you exactly what to fix, in plain language,
instead of raising a traceback.

Other commands:

```sh
./run.sh check               # preflight: credentials, Kalshi, markets, data feed
./run.sh sweep               # test other thresholds against data already collected
./run.sh report              # validation report + per-trade ledger
./run.sh report --stake 25   # same, with P&L scaled to $25 per trade
./run.sh health              # quick pulse check
./run.sh reset-breaker       # clear a tripped daily-loss breaker (manual)
```

To stop live orders instantly without touching the process: `touch ./HALT`
(the kill-switch file; delete it to resume). Data collection keeps running.

## What the market actually looks like (measured, 2026-07)

Two numbers worth knowing before tuning anything, both from live Kalshi books:

**Spreads depend entirely on how far out the game is.** Across 95 open MLB
markets, by time to first pitch:

| time to first pitch | n | median spread | wider than 3c |
|---|---|---|---|
| in progress / <3h | 29 | 1c | 0% |
| 12–48h | 44 | 1c | 0% |
| >48h out | 22 | 5c | 55% |

The liquid window is *tight*. That cuts both ways: there is no fat spread to
collect by making markets, and no spread-driven illusion of edge either — but
the far-dated books are wide enough to manufacture one, which is what
`risk.max_spread_cents` (default 3) keeps the bot out of.

**The model is calibrated but not sharp.** Over the first 52 settled markets the
sports model's predictions tracked outcomes well at the extremes (predicted 3%
happened 5%; predicted 97% happened 95%) while its Brier score sat at 0.25 —
i.e. it agrees with the market rather than beating it. Its one visible edge over
itself is late: Brier drops to 0.17 inside the last two hours, which is the
live-score clamp doing its job. Whether that beats the *market's* in-game price
is a different question, and the one the strategy section answers.

## Choosing thresholds without waiting: `sweep`

```sh
./run.sh sweep --config config/sports.yaml
```

Every decision is stored with its probability, price, spread and eventual
outcome, so "what would a 3c minimum edge and a 3c spread cap have done?" is a
query rather than another week of collecting. The sweep re-decides which
recorded candidates a different threshold would have taken, one entry per market,
and scores them.

It sweeps the *gates*, not the model — and it splits the history in half by date
and shows both, because the best cell of a table fitted on the same data is not
a discovery. A setting that only works in one half is noise, and the output says
so.

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

```sh
./install-autostart.sh              # install and start
./install-autostart.sh --dry-run    # print what it would write, change nothing
./install-autostart.sh --uninstall  # stop and remove
```

That installs one `launchd` agent per config file present (weather, sports),
each with its own label and its own log files, filling in absolute paths from
the checkout so there is nothing to hand-edit. It refuses to install while a
collector is running in a Terminal — two collectors on one database would poll
and write everything twice — and it stops its own agents first, so re-running it
after a `git pull` is the supported way to pick up new code.

A hand-editable template is still at `launchd/com.projectrebound.kalshibot.plist`
if you would rather wire it up yourself.

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

## Troubleshooting

**`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`** — a
python.org macOS install that never wired up its CA roots. The bot now prefers
`certifi`'s bundle automatically (installed by `setup.sh`), so re-running
`./setup.sh` fixes it. Python's own installer also ships a fix:

```sh
/Applications/Python\ 3.x/Install\ Certificates.command   # match your version
```

Certificate verification is never disabled to work around this.

**Warnings from a single provider** are normal and safe — a provider that fails
is simply left out of that cycle's ensemble, and the model uses whichever
sources did respond. Persistent failures from *every* provider mean a network
or certificate problem, not a market problem.

**`HTTP Error 400` from an Open-Meteo member** — the model identifier is wrong
or has been renamed. Errors now carry the API's own explanation, so the log
will name the offending value. Confirm the current name by querying the API
directly and reading the JSON `reason`:

```sh
curl "https://api.open-meteo.com/v1/forecast?latitude=41.786&longitude=-87.752&daily=temperature_2m_max&timezone=auto&models=gfs_hrrr"
```

then update `_OPEN_METEO_MODELS` in `clients/forecast_providers.py`.

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
execution disabled — for **both** families.

The sports tests also pin the things that would silently price the wrong game:
that the YES team comes from the ticker and not the sub-titles (which are
identical on both sides of a live market), that MLB tickers carry a start time and
NFL ones do not, that a market matched to no game yields no opinion rather than a
guess, that the two sides of a game always sum to 1, and that a started game with
a stale score feed is declined.

## Out of scope for Phase 1 (deliberately)

Deposits/withdrawals (permanently), crypto/election markets, sports markets other
than game winners (totals and spreads are parsed and then *declined*, not
half-understood), any size above 1 contract, Kelly/bankroll sizing, cross-venue
arbitrage, machine-learned models (both models are explicit so their mistakes are
legible), and any web UI or dashboard — this phase is CLI only.
