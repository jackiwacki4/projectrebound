import { describe, expect, it } from "vitest";
import { sameMarketArb } from "../src/strategies/sameMarketArb.js";
import type { StrategyContext } from "../src/strategies/types.js";
import type { Market } from "../src/types.js";

function market(overrides: Partial<Market> = {}): Market {
  return {
    ticker: "TICKER-1",
    eventTicker: "EVENT-1",
    title: "Test market",
    status: "open",
    yesBidCents: 40,
    yesAskCents: 45,
    noBidCents: 50,
    noAskCents: 53,
    volume: 100,
    closeTime: new Date().toISOString(),
    ...overrides,
  };
}

function contextWith(m: Market, minArbEdgeCents = 1): StrategyContext {
  return {
    markets: new Map([[m.ticker, m]]),
    events: new Map(),
    orderbooks: new Map(),
    minArbEdgeCents,
  };
}

describe("sameMarketArb", () => {
  it("flags a market where YES ask + NO ask < 100c by at least the edge floor", () => {
    const m = market({ yesAskCents: 45, noAskCents: 53 }); // combined 98c -> 2c edge
    const intents = sameMarketArb.onMarketUpdate!(contextWith(m, 1));

    expect(intents).toHaveLength(2);
    expect(intents.find((i) => i.side === "yes")).toMatchObject({
      ticker: "TICKER-1",
      action: "buy",
      limitPriceCents: 45,
    });
    expect(intents.find((i) => i.side === "no")).toMatchObject({
      ticker: "TICKER-1",
      action: "buy",
      limitPriceCents: 53,
    });
  });

  it("does not flag a market priced at or above 100c combined", () => {
    const m = market({ yesAskCents: 50, noAskCents: 51 }); // combined 101c
    const intents = sameMarketArb.onMarketUpdate!(contextWith(m, 1));
    expect(intents).toHaveLength(0);
  });

  it("does not flag an edge smaller than the configured floor", () => {
    const m = market({ yesAskCents: 49, noAskCents: 50 }); // combined 99c -> 1c edge
    const intents = sameMarketArb.onMarketUpdate!(contextWith(m, 2));
    expect(intents).toHaveLength(0);
  });

  it("ignores markets that are not open", () => {
    const m = market({ status: "closed", yesAskCents: 40, noAskCents: 40 });
    const intents = sameMarketArb.onMarketUpdate!(contextWith(m, 1));
    expect(intents).toHaveLength(0);
  });

  it("sizes using available depth from the order book when present", () => {
    const m = market({ yesAskCents: 45, noAskCents: 53 });
    const ctx = contextWith(m, 1);
    ctx.orderbooks.set(m.ticker, {
      ticker: m.ticker,
      yes: [{ priceCents: 40, count: 3 }],
      no: [{ priceCents: 50, count: 7 }],
    });
    const intents = sameMarketArb.onMarketUpdate!(ctx);
    // size is min(noDepth-for-yes-leg=7, yesDepth-for-no-leg=3) = 3
    expect(intents.every((i) => i.count === 3)).toBe(true);
  });
});
