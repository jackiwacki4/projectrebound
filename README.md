# projectrebound

A Kalshi (US-regulated prediction market) scanning bot: it watches every open
market for two kinds of edge and can act faster than a human clicking through
the UI.

**Starts in paper mode.** No real order is ever sent to Kalshi until you flip
two separate config switches (see below). Read this whole file before you do.

## What it does

- **Same-market arbitrage**: on Kalshi, exactly one of YES/NO settles at $1 on
  every market. If you can buy both for less than $1 combined, that gap is
  riskless profit at settlement (before fees).
- **Multi-outcome arbitrage**: for an event Kalshi marks mutually exclusive
  (e.g. "who wins X" with one market per candidate), if buying one YES
  contract on every leg costs less than $1 total, exactly one leg pays $1 --
  same idea, spread across the whole event.
- **News-reaction alerting**: polls RSS feeds, token-matches headlines against
  open market titles, and logs a loud "go look at this" alert. It deliberately
  does **not** auto-trade on this -- keyword overlap tells you a market might
  be relevant, not which direction (YES/NO) the news pushes it. Wiring that up
  for real needs actual sentiment/impact scoring, which isn't in scope here.

Both arbitrage scanners run continuously against real-time order book data
over Kalshi's WebSocket feed, plus a REST poll for the full market/event list.

## Two independent safety switches

| Switch | Values | What it controls |
|---|---|---|
| `KALSHI_ENVIRONMENT` | `demo` \| `production` | Which Kalshi API you talk to. `demo` is Kalshi's own sandbox with fake money. |
| `TRADING_MODE` | `paper` \| `live` | Whether orders actually get submitted. `paper` simulates fills locally against real prices; nothing is ever sent to Kalshi. |

`TRADING_MODE=live` additionally requires `LIVE_TRADING_CONFIRMED=true` --
the process refuses to start in live mode without it. That's a deliberate
second switch so going live is never an accident.

Recommended path: `demo` + `paper` first, then `production` + `paper` (real
prices, still simulated fills) to see if the edges you're finding are real and
big enough after fees, and only then `production` + `live` with tiny position
caps.

## Setup

```sh
npm install
cp .env.example .env
```

1. Create a Kalshi API key at https://kalshi.com/account/profile -> API Keys
   (use a demo account first). The private key PEM is shown **once** --
   save it immediately to the path you set in `KALSHI_PRIVATE_KEY_PATH`
   (default `./secrets/kalshi_private_key.pem`, gitignored).
2. Fill in `KALSHI_API_KEY_ID` in `.env`.
3. Adjust the risk limits in `.env` (all in cents) to whatever you're
   actually willing to lose.

```sh
npm run dev     # run with auto-reload
npm start       # run once, no reload
npm test        # vitest
npm run build   # compile to dist/
```

## Before you ever set `TRADING_MODE=live`

The order-placement request in `src/kalshi/restClient.ts::createOrder` was
written from Kalshi's API docs at generation time, and different sources
disagreed on the exact field names for order creation (the docs site is
JS-rendered, and different snapshots looked like different API versions).
The **read** endpoints (markets, events, order book) are stable and were
cross-checked, so paper mode -- which only ever reads -- doesn't depend on
that risk. Live mode does. Before flipping the live switch:

1. Diff `createOrder()`'s request body against the current
   https://docs.kalshi.com/api-reference/orders/create-order-v2
2. Place one small manual order against `KALSHI_ENVIRONMENT=demo` with
   `TRADING_MODE=live` and confirm it behaves as expected.
3. Only then point at `production`.

## Architecture

```
src/
  config.ts              env-driven config, fails fast on inconsistent switches
  kalshi/
    auth.ts               RSA-PSS request signing
    restClient.ts         markets/events/order book reads, order placement
    wsClient.ts           real-time order book via WebSocket, auto-reconnect
  strategies/
    sameMarketArb.ts       YES+NO < $1 on one market
    multiOutcomeArb.ts     sum of YES legs < $1 across a mutually-exclusive event
    newsReaction.ts        headline/market keyword match -> alert only
  news/newsFeed.ts         RSS polling, dedup, "priming pass" to skip backlog
  portfolio/
    paperPortfolio.ts      simulated cash/positions/realized P&L
    store.ts               JSON-file persistence (./data/, gitignored)
  risk/riskManager.ts      position/event caps, order-rate limit, daily-loss kill switch
  engine/
    scanner.ts             wires market data + news into strategies
    executor.ts            dispatches to paper portfolio or live order placement
  cli.ts                   entrypoint
```

Adding a strategy: implement `Strategy` from `src/strategies/types.ts` and
add it to the list in `src/cli.ts`. It gets the full market/event/order-book
snapshot on every update and returns `OrderIntent[]`; the risk manager sizes
and filters before anything reaches the executor.

## What this is not

- Not a guaranteed-profit system. Riskless-in-theory arbitrage still loses to
  trading fees, stale prices, and thin order-book depth that disappears
  before your order lands.
- Not backtested. It scans live/demo data only; there's no historical replay
  harness here yet.
- Not multi-venue. Built for Kalshi specifically (Polymarket doesn't serve US
  users, which is the whole reason to use Kalshi instead); a second exchange
  would need its own adapter behind the same `Market`/`Orderbook` types.
