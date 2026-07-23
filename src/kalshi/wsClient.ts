import WebSocket from "ws";
import { buildAuthHeaders } from "./auth.js";
import type { Config } from "../config.js";
import type { Logger } from "../logger.js";
import type { Orderbook, OrderbookLevel } from "../types.js";

type OrderbookListener = (orderbook: Orderbook) => void;

interface SnapshotMsg {
  type: "orderbook_snapshot";
  msg: { market_ticker: string; yes?: [number, number][]; no?: [number, number][] };
}
interface DeltaMsg {
  type: "orderbook_delta";
  msg: { market_ticker: string; side: "yes" | "no"; price: number; delta: number };
}
type InboundMsg = SnapshotMsg | DeltaMsg | { type: string; [key: string]: unknown };

/**
 * Maintains local order books via Kalshi's WebSocket feed. Reconnects with
 * backoff and re-subscribes to whatever tickers were being watched.
 */
export class KalshiWebSocketClient {
  private ws?: WebSocket;
  private nextRequestId = 1;
  private readonly subscribedTickers = new Set<string>();
  private readonly orderbooks = new Map<string, Orderbook>();
  private reconnectAttempt = 0;
  private closedByUser = false;

  constructor(
    private readonly config: Config,
    private readonly logger: Logger,
    private readonly onUpdate: OrderbookListener
  ) {}

  connect(): void {
    this.closedByUser = false;
    const { apiKeyId, privateKeyPem } = this.config.kalshi;
    if (!apiKeyId || !privateKeyPem) {
      throw new Error("Kalshi API credentials missing; cannot open WebSocket.");
    }
    const path = "/trade-api/ws/v2";
    const headers = buildAuthHeaders(apiKeyId, privateKeyPem, "GET", path);
    this.ws = new WebSocket(this.config.kalshi.wsBaseUrl, { headers });

    this.ws.on("open", () => {
      this.reconnectAttempt = 0;
      this.logger.info("Kalshi WebSocket connected");
      if (this.subscribedTickers.size > 0) {
        this.sendSubscribe([...this.subscribedTickers]);
      }
    });

    this.ws.on("message", (data) => this.handleMessage(data.toString()));

    this.ws.on("close", (code) => {
      this.logger.warn(`Kalshi WebSocket closed (${code})`);
      if (!this.closedByUser) this.scheduleReconnect();
    });

    this.ws.on("error", (err) => {
      this.logger.error("Kalshi WebSocket error", { error: String(err) });
    });
  }

  close(): void {
    this.closedByUser = true;
    this.ws?.close();
  }

  subscribeMarkets(tickers: string[]): void {
    const fresh = tickers.filter((t) => !this.subscribedTickers.has(t));
    if (fresh.length === 0) return;
    fresh.forEach((t) => this.subscribedTickers.add(t));
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.sendSubscribe(fresh);
    }
  }

  getOrderbook(ticker: string): Orderbook | undefined {
    return this.orderbooks.get(ticker);
  }

  private sendSubscribe(tickers: string[]): void {
    const cmd = {
      id: this.nextRequestId++,
      cmd: "subscribe",
      params: { channels: ["orderbook_delta"], market_tickers: tickers },
    };
    this.ws?.send(JSON.stringify(cmd));
  }

  private scheduleReconnect(): void {
    this.reconnectAttempt += 1;
    const delayMs = Math.min(30_000, 1000 * 2 ** this.reconnectAttempt);
    this.logger.info(`Reconnecting to Kalshi WebSocket in ${delayMs}ms`);
    setTimeout(() => this.connect(), delayMs);
  }

  private handleMessage(raw: string): void {
    let msg: InboundMsg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    if (msg.type === "orderbook_snapshot") {
      const { market_ticker, yes, no } = (msg as SnapshotMsg).msg;
      const book: Orderbook = {
        ticker: market_ticker,
        yes: toLevels(yes),
        no: toLevels(no),
      };
      this.orderbooks.set(market_ticker, book);
      this.onUpdate(book);
      return;
    }

    if (msg.type === "orderbook_delta") {
      const { market_ticker, side, price, delta } = (msg as DeltaMsg).msg;
      const book = this.orderbooks.get(market_ticker);
      if (!book) return; // delta before snapshot; drop, snapshot will resync
      applyDelta(book[side], price, delta);
      this.onUpdate(book);
    }
  }
}

function toLevels(pairs: [number, number][] | undefined): OrderbookLevel[] {
  return (pairs ?? [])
    .map(([priceCents, count]) => ({ priceCents, count }))
    .sort((a, b) => b.priceCents - a.priceCents);
}

function applyDelta(levels: OrderbookLevel[], priceCents: number, delta: number): void {
  const idx = levels.findIndex((l) => l.priceCents === priceCents);
  if (idx === -1) {
    if (delta > 0) {
      levels.push({ priceCents, count: delta });
      levels.sort((a, b) => b.priceCents - a.priceCents);
    }
    return;
  }
  levels[idx].count += delta;
  if (levels[idx].count <= 0) levels.splice(idx, 1);
}
