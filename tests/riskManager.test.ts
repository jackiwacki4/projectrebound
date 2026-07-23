import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { RiskManager } from "../src/risk/riskManager.js";
import { PaperPortfolio } from "../src/portfolio/paperPortfolio.js";
import { PortfolioStore } from "../src/portfolio/store.js";
import { createLogger } from "../src/logger.js";
import { config as baseConfig } from "../src/config.js";
import type { Config } from "../src/config.js";
import type { Market, OrderIntent } from "../src/types.js";

const logger = createLogger("error");
let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "projectrebound-risk-test-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function testConfig(overrides: Partial<Config["risk"]> = {}): Config {
  return {
    ...baseConfig,
    risk: { ...baseConfig.risk, ...overrides },
  };
}

function market(overrides: Partial<Market> = {}): Market {
  return {
    ticker: "T-1",
    eventTicker: "E-1",
    title: "Test",
    status: "open",
    yesBidCents: 40,
    yesAskCents: 45,
    noBidCents: 50,
    noAskCents: 53,
    volume: 1,
    closeTime: new Date().toISOString(),
    ...overrides,
  };
}

function intent(overrides: Partial<OrderIntent> = {}): OrderIntent {
  return {
    ticker: "T-1",
    side: "yes",
    action: "buy",
    count: 100,
    limitPriceCents: 45,
    strategy: "test",
    reason: "test",
    ...overrides,
  };
}

function newRiskManager(cfg: Config): { risk: RiskManager; portfolio: PaperPortfolio } {
  const store = new PortfolioStore(join(dir, "portfolio.json"), join(dir, "trades.jsonl"));
  const portfolio = new PaperPortfolio(cfg.paper.startingCashCents, store, logger);
  return { risk: new RiskManager(cfg, logger, portfolio), portfolio };
}

describe("RiskManager", () => {
  it("clamps order size down to stay within the per-market position cap", () => {
    const cfg = testConfig({ maxPositionCents: 900 });
    const { risk } = newRiskManager(cfg);
    const markets = new Map([["T-1", market()]]);

    const admitted = risk.admit([intent({ count: 100, limitPriceCents: 45 })], markets);

    expect(admitted).toHaveLength(1);
    expect(admitted[0].count).toBe(20); // floor(900 / 45)
  });

  it("drops an intent entirely once the position cap is already full", () => {
    const cfg = testConfig({ maxPositionCents: 100 });
    const { risk, portfolio } = newRiskManager(cfg);
    const markets = new Map([["T-1", market()]]);

    portfolio.applyFill({
      ticker: "T-1",
      side: "yes",
      action: "buy",
      count: 2,
      priceCents: 45,
      strategy: "seed",
      reason: "seed",
      ts: Date.now(),
    }); // 90c of 100c cap used

    const admitted = risk.admit([intent({ count: 10, limitPriceCents: 45 })], markets);
    expect(admitted).toHaveLength(0);
  });

  it("trips the daily loss kill switch and blocks further orders", () => {
    const cfg = testConfig({ maxDailyLossCents: 100, maxPositionCents: 1_000_000 });
    const { risk, portfolio } = newRiskManager(cfg);
    const flatMarkets = new Map([["T-1", market()]]);

    // Establish the same-day P&L baseline before any loss happens, exactly
    // like production does on the first scan after startup.
    risk.admit([], flatMarkets);

    portfolio.applyFill({
      ticker: "T-1",
      side: "yes",
      action: "buy",
      count: 100,
      priceCents: 45,
      strategy: "seed",
      reason: "seed",
      ts: Date.now(),
    });

    // Mark the position down hard: bought at 45c, now worth 10c -> -3500c unrealized.
    const markedDownMarkets = new Map([["T-1", market({ yesBidCents: 10 })]]);

    const admitted = risk.admit([intent({ count: 1, limitPriceCents: 45 })], markedDownMarkets);
    expect(admitted).toHaveLength(0);
  });

  it("enforces the per-event exposure cap across multiple markets in the same event", () => {
    const cfg = testConfig({ maxEventExposureCents: 100, maxPositionCents: 1_000_000 });
    const { risk, portfolio } = newRiskManager(cfg);
    const markets = new Map([
      ["T-1", market({ ticker: "T-1", eventTicker: "E-1" })],
      ["T-2", market({ ticker: "T-2", eventTicker: "E-1" })],
    ]);

    portfolio.applyFill({
      ticker: "T-1",
      side: "yes",
      action: "buy",
      count: 2,
      priceCents: 45,
      strategy: "seed",
      reason: "seed",
      ts: Date.now(),
    }); // 90c already committed to event E-1

    const admitted = risk.admit(
      [intent({ ticker: "T-2", count: 1, limitPriceCents: 45 })],
      markets
    );
    expect(admitted).toHaveLength(0);
  });
});
