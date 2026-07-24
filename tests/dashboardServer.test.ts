import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DashboardServer } from "../src/web/server.js";
import { PaperPortfolio } from "../src/portfolio/paperPortfolio.js";
import { PortfolioStore } from "../src/portfolio/store.js";
import { RiskManager } from "../src/risk/riskManager.js";
import { ScannerEngine } from "../src/engine/scanner.js";
import { KalshiRestClient } from "../src/kalshi/restClient.js";
import { Executor } from "../src/engine/executor.js";
import { createLogger } from "../src/logger.js";
import { config as baseConfig } from "../src/config.js";
import type { Config } from "../src/config.js";

const logger = createLogger("error");
let dir: string;
let server: DashboardServer;
let portfolio: PaperPortfolio;
let store: PortfolioStore;
let baseUrl: string;

function basicHeader(user: string, pass: string): string {
  return "Basic " + Buffer.from(`${user}:${pass}`).toString("base64");
}

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "projectrebound-dashboard-test-"));
  const cfg: Config = {
    ...baseConfig,
    dashboard: {
      enabled: true,
      host: "127.0.0.1",
      port: 0, // ephemeral, real port read back after start()
      username: "tester",
      password: "s3cret",
    },
  };

  store = new PortfolioStore(join(dir, "portfolio.json"), join(dir, "trades.jsonl"));
  portfolio = new PaperPortfolio(cfg.paper.startingCashCents, store, logger);
  const riskManager = new RiskManager(cfg, logger, portfolio);
  const restClient = new KalshiRestClient(cfg, logger);
  const executor = new Executor(cfg, logger, portfolio, restClient, store);
  const scanner = new ScannerEngine(cfg, logger, restClient, riskManager, executor, []);

  server = new DashboardServer(cfg, logger, portfolio, store, riskManager, scanner, restClient);
  await server.start();
  baseUrl = `http://127.0.0.1:${server.getPort()}`;
});

afterEach(() => {
  server.stop();
  rmSync(dir, { recursive: true, force: true });
});

describe("DashboardServer", () => {
  it("rejects requests without credentials with 401", async () => {
    const res = await fetch(`${baseUrl}/api/status`);
    expect(res.status).toBe(401);
  });

  it("rejects requests with the wrong password", async () => {
    const res = await fetch(`${baseUrl}/api/status`, {
      headers: { Authorization: basicHeader("tester", "wrong") },
    });
    expect(res.status).toBe(401);
  });

  it("serves /api/status with the right credentials", async () => {
    const res = await fetch(`${baseUrl}/api/status`, {
      headers: { Authorization: basicHeader("tester", "s3cret") },
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toMatchObject({ mode: "paper", riskTripped: false, trackedMarkets: 0 });
  });

  it("serves the current paper portfolio state via /api/portfolio", async () => {
    portfolio.applyFill({
      ticker: "T-1",
      side: "yes",
      action: "buy",
      count: 10,
      priceCents: 45,
      strategy: "test",
      reason: "test",
      ts: Date.now(),
    });

    const res = await fetch(`${baseUrl}/api/portfolio`, {
      headers: { Authorization: basicHeader("tester", "s3cret") },
    });
    const body = await res.json();
    expect(body.source).toBe("paper");
    expect(body.cashCents).toBe(baseConfig.paper.startingCashCents - 450);
    expect(body.positions).toHaveLength(1);
    expect(body.positions[0]).toMatchObject({ ticker: "T-1", yesCount: 10, yesCostCents: 450 });
  });

  it("serves the trade ledger via /api/trades", async () => {
    store.appendTrade({
      ticker: "T-1",
      side: "yes",
      action: "buy",
      count: 5,
      priceCents: 40,
      strategy: "test",
      reason: "test",
      ts: 1000,
    });

    const res = await fetch(`${baseUrl}/api/trades`, {
      headers: { Authorization: basicHeader("tester", "s3cret") },
    });
    const trades = await res.json();
    expect(trades).toHaveLength(1);
    expect(trades[0]).toMatchObject({ ticker: "T-1", count: 5, priceCents: 40 });
  });

  it("serves the dashboard HTML at /", async () => {
    const res = await fetch(baseUrl, {
      headers: { Authorization: basicHeader("tester", "s3cret") },
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/html");
    const html = await res.text();
    expect(html).toContain("projectrebound");
  });

  it("returns 404 for an unknown path", async () => {
    const res = await fetch(`${baseUrl}/nope`, {
      headers: { Authorization: basicHeader("tester", "s3cret") },
    });
    expect(res.status).toBe(404);
  });
});
