import type { OrderIntent } from "../types.js";
import type { Strategy, StrategyContext } from "./types.js";

const DEFAULT_DEPTH_FALLBACK = 10;
const MAX_SIZE_PER_SIGNAL = 50;

/**
 * Kalshi YES/NO contracts on the same market always settle so that exactly
 * one side pays $1. If you can buy 1 YES + 1 NO for less than $1 combined,
 * that spread is riskless profit at settlement (before fees -- Kalshi charges
 * a per-contract trading fee that eats into thin edges, so treat
 * minArbEdgeCents as a floor, not a guarantee of net profit).
 */
export const sameMarketArb: Strategy = {
  name: "same-market-arb",
  onMarketUpdate(ctx: StrategyContext): OrderIntent[] {
    const intents: OrderIntent[] = [];

    for (const market of ctx.markets.values()) {
      if (market.status !== "open") continue;
      const ob = ctx.orderbooks.get(market.ticker);

      const yesAskCents = market.yesAskCents;
      const noAskCents = market.noAskCents;
      if (!yesAskCents || !noAskCents) continue;

      const combinedCostCents = yesAskCents + noAskCents;
      const edgeCents = 100 - combinedCostCents;
      if (edgeCents < ctx.minArbEdgeCents) continue;

      const yesDepth = ob?.no[0]?.count ?? DEFAULT_DEPTH_FALLBACK;
      const noDepth = ob?.yes[0]?.count ?? DEFAULT_DEPTH_FALLBACK;
      const size = Math.max(1, Math.min(yesDepth, noDepth, MAX_SIZE_PER_SIGNAL));

      const reason = `YES ask ${yesAskCents}c + NO ask ${noAskCents}c = ${combinedCostCents}c, edge ${edgeCents}c`;

      intents.push({
        ticker: market.ticker,
        side: "yes",
        action: "buy",
        count: size,
        limitPriceCents: yesAskCents,
        strategy: sameMarketArb.name,
        reason,
      });
      intents.push({
        ticker: market.ticker,
        side: "no",
        action: "buy",
        count: size,
        limitPriceCents: noAskCents,
        strategy: sameMarketArb.name,
        reason,
      });
    }

    return intents;
  },
};
