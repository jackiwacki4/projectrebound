export interface Market {
  ticker: string;
  eventTicker: string;
  title: string;
  subtitle?: string;
  status: string;
  yesBidCents: number;
  yesAskCents: number;
  noBidCents: number;
  noAskCents: number;
  volume: number;
  closeTime: string;
}

export interface EventInfo {
  eventTicker: string;
  title: string;
  mutuallyExclusive: boolean;
  marketTickers: string[];
}

export interface OrderbookLevel {
  priceCents: number;
  count: number;
}

export interface Orderbook {
  ticker: string;
  yes: OrderbookLevel[]; // bids on YES, best first
  no: OrderbookLevel[]; // bids on NO, best first
}

export interface NewsItem {
  source: string;
  headline: string;
  url: string;
  publishedAt: string;
}

export type Side = "yes" | "no";
export type TradeAction = "buy" | "sell";

export interface OrderIntent {
  ticker: string;
  side: Side;
  action: TradeAction;
  count: number;
  limitPriceCents: number;
  strategy: string;
  reason: string;
}

export interface Fill {
  ticker: string;
  side: Side;
  action: TradeAction;
  count: number;
  priceCents: number;
  strategy: string;
  reason: string;
  ts: number;
}
