import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { appendFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PortfolioStore } from "../src/portfolio/store.js";
import type { Fill } from "../src/types.js";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "projectrebound-store-test-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function fill(overrides: Partial<Fill> = {}): Fill {
  return {
    ticker: "T-1",
    side: "yes",
    action: "buy",
    count: 1,
    priceCents: 50,
    strategy: "test",
    reason: "test",
    ts: Date.now(),
    ...overrides,
  };
}

describe("PortfolioStore.readTrades", () => {
  it("returns an empty array when no trades have been recorded", () => {
    const store = new PortfolioStore(join(dir, "portfolio.json"), join(dir, "trades.jsonl"));
    expect(store.readTrades(50)).toEqual([]);
  });

  it("returns trades most-recent-first", () => {
    const store = new PortfolioStore(join(dir, "portfolio.json"), join(dir, "trades.jsonl"));
    store.appendTrade(fill({ ticker: "A", ts: 1000 }));
    store.appendTrade(fill({ ticker: "B", ts: 2000 }));
    store.appendTrade(fill({ ticker: "C", ts: 3000 }));

    const trades = store.readTrades(50);
    expect(trades.map((t) => t.ticker)).toEqual(["C", "B", "A"]);
  });

  it("respects the limit", () => {
    const store = new PortfolioStore(join(dir, "portfolio.json"), join(dir, "trades.jsonl"));
    for (let i = 0; i < 5; i++) store.appendTrade(fill({ ticker: `T-${i}`, ts: i * 1000 }));

    expect(store.readTrades(2)).toHaveLength(2);
  });

  it("pages backward in time using the before cursor", () => {
    const store = new PortfolioStore(join(dir, "portfolio.json"), join(dir, "trades.jsonl"));
    store.appendTrade(fill({ ticker: "A", ts: 1000 }));
    store.appendTrade(fill({ ticker: "B", ts: 2000 }));
    store.appendTrade(fill({ ticker: "C", ts: 3000 }));

    const firstPage = store.readTrades(2);
    expect(firstPage.map((t) => t.ticker)).toEqual(["C", "B"]);

    const secondPage = store.readTrades(2, firstPage[firstPage.length - 1].ts);
    expect(secondPage.map((t) => t.ticker)).toEqual(["A"]);
  });

  it("skips a malformed line instead of failing the whole read", () => {
    const store = new PortfolioStore(join(dir, "portfolio.json"), join(dir, "trades.jsonl"));
    store.appendTrade(fill({ ticker: "A", ts: 1000 }));
    // Simulate a partially-written line from a crash mid-append.
    appendFileSync(join(dir, "trades.jsonl"), '{"ticker":"broken", incomplete\n');
    store.appendTrade(fill({ ticker: "B", ts: 2000 }));

    const trades = store.readTrades(50);
    expect(trades.map((t) => t.ticker)).toEqual(["B", "A"]);
  });
});
