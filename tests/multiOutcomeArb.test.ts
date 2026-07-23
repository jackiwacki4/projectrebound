import { describe, expect, it } from "vitest";
import { multiOutcomeArb } from "../src/strategies/multiOutcomeArb.js";
import type { StrategyContext } from "../src/strategies/types.js";
import type { EventInfo, Market } from "../src/types.js";

function leg(ticker: string, yesAskCents: number): Market {
  return {
    ticker,
    eventTicker: "EVENT-X",
    title: `Candidate ${ticker}`,
    status: "open",
    yesBidCents: yesAskCents - 2,
    yesAskCents,
    noBidCents: 100 - yesAskCents - 2,
    noAskCents: 100 - yesAskCents + 2,
    volume: 100,
    closeTime: new Date().toISOString(),
  };
}

function baseContext(legs: Market[], event: EventInfo, minArbEdgeCents = 1): StrategyContext {
  return {
    markets: new Map(legs.map((m) => [m.ticker, m])),
    events: new Map([[event.eventTicker, event]]),
    orderbooks: new Map(),
    minArbEdgeCents,
  };
}

describe("multiOutcomeArb", () => {
  it("flags a mutually-exclusive event whose legs sum to under 100c", () => {
    const legs = [leg("A", 30), leg("B", 30), leg("C", 30)]; // sums to 90c
    const event: EventInfo = {
      eventTicker: "EVENT-X",
      title: "Who wins",
      mutuallyExclusive: true,
      marketTickers: ["A", "B", "C"],
    };
    const intents = multiOutcomeArb.onMarketUpdate!(baseContext(legs, event, 1));
    expect(intents).toHaveLength(3);
    expect(intents.every((i) => i.side === "yes" && i.action === "buy")).toBe(true);
  });

  it("does not flag when legs sum to 100c or more", () => {
    const legs = [leg("A", 40), leg("B", 35), leg("C", 26)]; // sums to 101c
    const event: EventInfo = {
      eventTicker: "EVENT-X",
      title: "Who wins",
      mutuallyExclusive: true,
      marketTickers: ["A", "B", "C"],
    };
    const intents = multiOutcomeArb.onMarketUpdate!(baseContext(legs, event, 1));
    expect(intents).toHaveLength(0);
  });

  it("ignores events not marked mutually exclusive", () => {
    const legs = [leg("A", 30), leg("B", 30)];
    const event: EventInfo = {
      eventTicker: "EVENT-X",
      title: "Unrelated pair",
      mutuallyExclusive: false,
      marketTickers: ["A", "B"],
    };
    const intents = multiOutcomeArb.onMarketUpdate!(baseContext(legs, event, 1));
    expect(intents).toHaveLength(0);
  });

  it("skips an event if any leg is missing or closed", () => {
    const legs = [leg("A", 30), { ...leg("B", 30), status: "closed" }];
    const event: EventInfo = {
      eventTicker: "EVENT-X",
      title: "Who wins",
      mutuallyExclusive: true,
      marketTickers: ["A", "B", "C"], // C not present at all
    };
    const intents = multiOutcomeArb.onMarketUpdate!(baseContext(legs, event, 1));
    expect(intents).toHaveLength(0);
  });
});
