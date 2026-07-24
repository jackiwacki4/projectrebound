import { buildAuthHeaders } from "./auth.js";
import type { Config } from "../config.js";
import type { EventInfo, Market, Orderbook } from "../types.js";
import type { Logger } from "../logger.js";

interface RawMarket {
  ticker: string;
  event_ticker: string;
  title: string;
  subtitle?: string;
  status: string;
  yes_bid: number;
  yes_ask: number;
  no_bid: number;
  no_ask: number;
  volume: number;
  close_time: string;
}

interface RawEvent {
  event_ticker: string;
  title: string;
  mutually_exclusive: boolean;
}

function toMarket(raw: RawMarket): Market {
  return {
    ticker: raw.ticker,
    eventTicker: raw.event_ticker,
    title: raw.title,
    subtitle: raw.subtitle,
    status: raw.status,
    yesBidCents: raw.yes_bid,
    yesAskCents: raw.yes_ask,
    noBidCents: raw.no_bid,
    noAskCents: raw.no_ask,
    volume: raw.volume,
    closeTime: raw.close_time,
  };
}

export class KalshiRestClient {
  constructor(private readonly config: Config, private readonly logger: Logger) {}

  private requireCredentials(): { apiKeyId: string; privateKeyPem: string } {
    const { apiKeyId, privateKeyPem } = this.config.kalshi;
    if (!apiKeyId || !privateKeyPem) {
      throw new Error(
        "Kalshi API credentials missing. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH " +
          "(see .env.example) -- required even for read-only market data."
      );
    }
    return { apiKeyId, privateKeyPem };
  }

  private async request<T>(
    method: "GET" | "POST" | "DELETE",
    endpoint: string,
    body?: unknown
  ): Promise<T> {
    const { apiKeyId, privateKeyPem } = this.requireCredentials();
    const url = new URL(this.config.kalshi.restBaseUrl + endpoint);
    const headers = buildAuthHeaders(apiKeyId, privateKeyPem, method, url.pathname);

    const res = await fetch(url, {
      method,
      headers: {
        ...headers,
        "Content-Type": "application/json",
      },
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`Kalshi ${method} ${endpoint} -> ${res.status}: ${text}`);
    }
    return (await res.json()) as T;
  }

  async listOpenMarkets(): Promise<Market[]> {
    const markets: Market[] = [];
    let cursor: string | undefined;
    do {
      const qs = new URLSearchParams({ status: "open", limit: "200" });
      if (cursor) qs.set("cursor", cursor);
      const page = await this.request<{ markets: RawMarket[]; cursor?: string }>(
        "GET",
        `/markets?${qs.toString()}`
      );
      markets.push(...page.markets.map(toMarket));
      cursor = page.cursor || undefined;
    } while (cursor);
    this.logger.debug(`Fetched ${markets.length} open markets`);
    return markets;
  }

  async listEvents(): Promise<EventInfo[]> {
    const events: EventInfo[] = [];
    let cursor: string | undefined;
    do {
      const qs = new URLSearchParams({ status: "open", limit: "200", with_nested_markets: "true" });
      if (cursor) qs.set("cursor", cursor);
      const page = await this.request<{
        events: (RawEvent & { markets?: RawMarket[] })[];
        cursor?: string;
      }>("GET", `/events?${qs.toString()}`);
      events.push(
        ...page.events.map((e) => ({
          eventTicker: e.event_ticker,
          title: e.title,
          mutuallyExclusive: e.mutually_exclusive,
          marketTickers: (e.markets ?? []).map((m) => m.ticker),
        }))
      );
      cursor = page.cursor || undefined;
    } while (cursor);
    return events;
  }

  async getOrderbook(ticker: string): Promise<Orderbook> {
    const raw = await this.request<{
      orderbook: { yes?: [number, number][]; no?: [number, number][] };
    }>("GET", `/markets/${ticker}/orderbook`);
    return {
      ticker,
      yes: (raw.orderbook.yes ?? []).map(([priceCents, count]) => ({ priceCents, count })),
      no: (raw.orderbook.no ?? []).map(([priceCents, count]) => ({ priceCents, count })),
    };
  }

  /**
   * Real account balance, in cents. UNVERIFIED SCHEMA (see createOrder's note
   * below) -- read-only, so the worst case is a wrong dashboard number, not a
   * bad trade. Confirm against https://docs.kalshi.com/api-reference/portfolio/get-balance
   * before trusting it for anything beyond a rough dashboard readout.
   */
  async getBalance(): Promise<{ balanceCents: number; portfolioValueCents: number }> {
    const raw = await this.request<{ balance: number; portfolio_value: number }>(
      "GET",
      "/portfolio/balance"
    );
    return { balanceCents: raw.balance, portfolioValueCents: raw.portfolio_value };
  }

  /**
   * Real open positions. Same unverified-schema caveat as getBalance().
   * `positionCount` per Kalshi's docs is signed (negative = net NO), but the
   * "_fp" (fixed-point) suffix on the raw field is ambiguous about scaling --
   * confirm against https://docs.kalshi.com/api-reference/portfolio/get-positions
   * before trusting the count, not just the sign.
   */
  async getPositions(): Promise<
    Array<{ ticker: string; positionCount: number; marketExposureCents: number; realizedPnlCents: number }>
  > {
    const raw = await this.request<{
      market_positions: Array<{
        ticker: string;
        position_fp: number;
        market_exposure_dollars: string;
        realized_pnl_dollars: string;
      }>;
    }>("GET", "/portfolio/positions");
    return raw.market_positions.map((p) => ({
      ticker: p.ticker,
      positionCount: p.position_fp,
      marketExposureCents: Math.round(Number(p.market_exposure_dollars) * 100),
      realizedPnlCents: Math.round(Number(p.realized_pnl_dollars) * 100),
    }));
  }

  /**
   * Places a real order. UNVERIFIED SCHEMA: written from Kalshi API docs that
   * conflicted between sources at the time this was generated (JS-rendered
   * docs site, possible v1/v2 field drift). Do NOT rely on this compiling
   * and running as proof it's correct -- diff this against
   * https://docs.kalshi.com/api-reference/orders/create-order-v2 yourself,
   * and test against KALSHI_ENVIRONMENT=demo first, before TRADING_MODE=live
   * ever touches real money.
   */
  async createOrder(order: {
    ticker: string;
    side: "yes" | "no";
    action: "buy" | "sell";
    count: number;
    limitPriceCents: number;
    clientOrderId: string;
  }): Promise<{ orderId: string }> {
    if (!this.config.trading.liveTradingConfirmed) {
      throw new Error("Refusing to place a live order: LIVE_TRADING_CONFIRMED is not true.");
    }
    const body = {
      ticker: order.ticker,
      client_order_id: order.clientOrderId,
      side: order.side,
      action: order.action,
      count: order.count,
      type: "limit",
      time_in_force: "immediate_or_cancel",
      ...(order.side === "yes"
        ? { yes_price: order.limitPriceCents }
        : { no_price: order.limitPriceCents }),
    };
    const res = await this.request<{ order: { order_id: string } }>(
      "POST",
      "/portfolio/orders",
      body
    );
    return { orderId: res.order.order_id };
  }
}
