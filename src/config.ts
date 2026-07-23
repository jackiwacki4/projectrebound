import "dotenv/config";
import { readFileSync } from "node:fs";

function bool(v: string | undefined, fallback: boolean): boolean {
  if (v === undefined) return fallback;
  return v.trim().toLowerCase() === "true";
}

function int(v: string | undefined, fallback: number): number {
  if (v === undefined || v.trim() === "") return fallback;
  const n = Number.parseInt(v, 10);
  if (Number.isNaN(n)) throw new Error(`Expected an integer, got "${v}"`);
  return n;
}

const environment = (process.env.KALSHI_ENVIRONMENT ?? "demo") as "demo" | "production";
if (environment !== "demo" && environment !== "production") {
  throw new Error(`KALSHI_ENVIRONMENT must be "demo" or "production", got "${environment}"`);
}

const tradingMode = (process.env.TRADING_MODE ?? "paper") as "paper" | "live";
if (tradingMode !== "paper" && tradingMode !== "live") {
  throw new Error(`TRADING_MODE must be "paper" or "live", got "${tradingMode}"`);
}

const liveTradingConfirmed = bool(process.env.LIVE_TRADING_CONFIRMED, false);
if (tradingMode === "live" && !liveTradingConfirmed) {
  throw new Error(
    "TRADING_MODE=live requires LIVE_TRADING_CONFIRMED=true as an explicit second switch. " +
      "Set it only once you have verified the order-placement schema in src/kalshi/restClient.ts " +
      "against the current Kalshi API docs -- it was written from docs that may have drifted."
  );
}

const restBaseUrl =
  environment === "production"
    ? "https://api.elections.kalshi.com/trade-api/v2"
    : "https://demo-api.kalshi.co/trade-api/v2";

const wsBaseUrl =
  environment === "production"
    ? "wss://api.elections.kalshi.com/trade-api/ws/v2"
    : "wss://demo-api.kalshi.co/trade-api/ws/v2";

function loadPrivateKey(): string | undefined {
  const path = process.env.KALSHI_PRIVATE_KEY_PATH;
  if (!path) return undefined;
  try {
    return readFileSync(path, "utf8");
  } catch {
    return undefined;
  }
}

export const config = {
  kalshi: {
    environment,
    restBaseUrl,
    wsBaseUrl,
    apiKeyId: process.env.KALSHI_API_KEY_ID ?? "",
    privateKeyPem: loadPrivateKey(),
  },
  trading: {
    mode: tradingMode,
    liveTradingConfirmed,
  },
  paper: {
    startingCashCents: int(process.env.STARTING_CASH_CENTS, 100_000),
  },
  risk: {
    maxPositionCents: int(process.env.MAX_POSITION_CENTS, 5_000),
    maxEventExposureCents: int(process.env.MAX_EVENT_EXPOSURE_CENTS, 20_000),
    maxDailyLossCents: int(process.env.MAX_DAILY_LOSS_CENTS, 10_000),
    maxOrdersPerMinute: int(process.env.MAX_ORDERS_PER_MINUTE, 20),
  },
  scanning: {
    marketPollIntervalMs: int(process.env.MARKET_POLL_INTERVAL_MS, 15_000),
    minArbEdgeCents: int(process.env.MIN_ARB_EDGE_CENTS, 1),
    newsPollIntervalMs: int(process.env.NEWS_POLL_INTERVAL_MS, 30_000),
    newsFeeds: (process.env.NEWS_FEEDS ?? "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  },
  logLevel: process.env.LOG_LEVEL ?? "info",
};

export type Config = typeof config;
