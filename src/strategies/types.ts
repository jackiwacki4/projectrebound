import type { EventInfo, Market, NewsItem, Orderbook, OrderIntent } from "../types.js";

export interface StrategyContext {
  markets: Map<string, Market>;
  events: Map<string, EventInfo>;
  orderbooks: Map<string, Orderbook>;
  minArbEdgeCents: number;
}

export interface Strategy {
  name: string;
  onMarketUpdate?(ctx: StrategyContext): OrderIntent[];
  onNews?(item: NewsItem, ctx: StrategyContext): OrderIntent[];
}
