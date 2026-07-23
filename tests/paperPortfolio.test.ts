import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PaperPortfolio } from "../src/portfolio/paperPortfolio.js";
import { PortfolioStore } from "../src/portfolio/store.js";
import { createLogger } from "../src/logger.js";
import type { Fill, Market } from "../src/types.js";

const logger = createLogger("error");
let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "projectrebound-test-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function newPortfolio(startingCashCents = 100_000): PaperPortfolio {
  const store = new PortfolioStore(join(dir, "portfolio.json"), join(dir, "trades.jsonl"));
  return new PaperPortfolio(startingCashCents, store, logger);
}

function buyFill(overrides: Partial<Fill> = {}): Fill {
  return {
    ticker: "T-1",
    side: "yes",
    action: "buy",
    count: 10,
    priceCents: 45,
    strategy: "test",
    reason: "test",
    ts: Date.now(),
    ...overrides,
  };
}

describe("PaperPortfolio", () => {
  it("debits cash and opens a position on a buy fill", () => {
    const portfolio = newPortfolio(100_000);
    portfolio.applyFill(buyFill({ count: 10, priceCents: 45 }));

    expect(portfolio.getState().cashCents).toBe(100_000 - 450);
    expect(portfolio.getPosition("T-1")).toMatchObject({ yesCount: 10, yesCostCents: 450 });
  });

  it("credits cash and realizes P&L on a sell fill", () => {
    const portfolio = newPortfolio(100_000);
    portfolio.applyFill(buyFill({ count: 10, priceCents: 45 })); // cost basis 4.5c/contract... 45c each
    portfolio.applyFill(
      buyFill({ action: "sell", count: 10, priceCents: 60 }) // sell at 60c, bought at 45c
    );

    expect(portfolio.getPosition("T-1").yesCount).toBe(0);
    expect(portfolio.getState().realizedPnlCents).toBeCloseTo(150, 5); // (60-45)*10
    expect(portfolio.getState().cashCents).toBe(100_000 - 450 + 600);
  });

  it("tracks YES and NO legs on the same ticker independently", () => {
    const portfolio = newPortfolio(100_000);
    portfolio.applyFill(buyFill({ side: "yes", count: 5, priceCents: 45 }));
    portfolio.applyFill(buyFill({ side: "no", count: 5, priceCents: 53 }));

    const position = portfolio.getPosition("T-1");
    expect(position.yesCount).toBe(5);
    expect(position.noCount).toBe(5);
    expect(portfolio.positionNotionalCents("T-1")).toBe(225 + 265);
  });

  it("persists state across instances via the store", () => {
    const store = new PortfolioStore(join(dir, "portfolio.json"), join(dir, "trades.jsonl"));
    const first = new PaperPortfolio(100_000, store, logger);
    first.applyFill(buyFill({ count: 10, priceCents: 45 }));

    const reloaded = new PaperPortfolio(100_000, store, logger);
    expect(reloaded.getPosition("T-1").yesCount).toBe(10);
    expect(reloaded.getState().cashCents).toBe(100_000 - 450);
  });

  it("computes unrealized P&L by marking open positions to the current best bid", () => {
    const portfolio = newPortfolio(100_000);
    portfolio.applyFill(buyFill({ count: 10, priceCents: 45 }));

    const markets = new Map<string, Market>([
      [
        "T-1",
        {
          ticker: "T-1",
          eventTicker: "E-1",
          title: "Test",
          status: "open",
          yesBidCents: 60,
          yesAskCents: 62,
          noBidCents: 38,
          noAskCents: 40,
          volume: 1,
          closeTime: new Date().toISOString(),
        },
      ],
    ]);

    expect(portfolio.unrealizedPnlCents(markets)).toBe(10 * 60 - 450); // 150
  });
});
