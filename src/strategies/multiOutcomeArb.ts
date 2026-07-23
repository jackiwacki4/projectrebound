import type { OrderIntent } from "../types.js";
import type { Strategy, StrategyContext } from "./types.js";

const DEFAULT_DEPTH_FALLBACK = 10;
const MAX_SIZE_PER_SIGNAL = 20;

/**
 * For an event whose markets are mutually exclusive and exhaustive (e.g.
 * "who wins the primary" with one market per candidate), exactly one YES
 * pays out across the whole set. If the sum of every leg's YES ask is under
 * $1, buying one YES contract on every leg locks in the gap as profit
 * (again, before fees -- and only genuinely riskless if the event really is
 * exhaustive; a market covering "other/field" that's missing breaks this).
 */
export const multiOutcomeArb: Strategy = {
  name: "multi-outcome-arb",
  onMarketUpdate(ctx: StrategyContext): OrderIntent[] {
    const intents: OrderIntent[] = [];

    for (const event of ctx.events.values()) {
      if (!event.mutuallyExclusive || event.marketTickers.length < 2) continue;

      const legs = event.marketTickers
        .map((ticker) => ctx.markets.get(ticker))
        .filter((m): m is NonNullable<typeof m> => !!m && m.status === "open");

      if (legs.length !== event.marketTickers.length) continue; // missing/closed leg, skip

      const totalCostCents = legs.reduce((sum, m) => sum + m.yesAskCents, 0);
      const edgeCents = 100 - totalCostCents;
      if (edgeCents < ctx.minArbEdgeCents) continue;

      const size = Math.max(
        1,
        Math.min(
          MAX_SIZE_PER_SIGNAL,
          ...legs.map((m) => ctx.orderbooks.get(m.ticker)?.no[0]?.count ?? DEFAULT_DEPTH_FALLBACK)
        )
      );

      const reason = `Event ${event.eventTicker}: ${legs.length} legs sum to ${totalCostCents}c, edge ${edgeCents}c`;

      for (const leg of legs) {
        intents.push({
          ticker: leg.ticker,
          side: "yes",
          action: "buy",
          count: size,
          limitPriceCents: leg.yesAskCents,
          strategy: multiOutcomeArb.name,
          reason,
        });
      }
    }

    return intents;
  },
};
