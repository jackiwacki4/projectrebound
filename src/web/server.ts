import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { checkBasicAuth } from "./auth.js";
import { DASHBOARD_HTML } from "./dashboardHtml.js";
import type { Config } from "../config.js";
import type { Logger } from "../logger.js";
import type { Market } from "../types.js";
import type { PaperPortfolio } from "../portfolio/paperPortfolio.js";
import type { PortfolioStore } from "../portfolio/store.js";
import type { RiskManager } from "../risk/riskManager.js";
import type { ScannerEngine } from "../engine/scanner.js";
import type { KalshiRestClient } from "../kalshi/restClient.js";

/**
 * Read-only monitoring dashboard: current portfolio, positions, and the
 * trade ledger, behind HTTP Basic Auth. Always binds to config.dashboard.host
 * (127.0.0.1 by default) -- it is never meant to listen on a LAN- or
 * internet-reachable interface directly. Reach it remotely via a tunnel
 * (Cloudflare Tunnel, Tailscale, an SSH port-forward) that you control,
 * not by changing this bind address.
 */
export class DashboardServer {
  private server?: Server;

  constructor(
    private readonly config: Config,
    private readonly logger: Logger,
    private readonly portfolio: PaperPortfolio,
    private readonly store: PortfolioStore,
    private readonly riskManager: RiskManager,
    private readonly scanner: ScannerEngine,
    private readonly restClient: KalshiRestClient
  ) {}

  start(): Promise<void> {
    return new Promise((resolve) => {
      this.server = createServer((req, res) => void this.handle(req, res));
      this.server.listen(this.config.dashboard.port, this.config.dashboard.host, () => {
        this.logger.info(
          `Dashboard listening on http://${this.config.dashboard.host}:${this.getPort()} ` +
            "(localhost-only -- use a tunnel for remote access)"
        );
        resolve();
      });
    });
  }

  /** The actual bound port -- useful when config.dashboard.port is 0 (ephemeral), e.g. in tests. */
  getPort(): number | undefined {
    const address = this.server?.address();
    return address && typeof address === "object" ? address.port : undefined;
  }

  stop(): void {
    this.server?.close();
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    if (
      !checkBasicAuth(req.headers.authorization, this.config.dashboard.username, this.config.dashboard.password)
    ) {
      res.writeHead(401, { "WWW-Authenticate": 'Basic realm="projectrebound"' });
      res.end("Authentication required");
      return;
    }

    const url = new URL(req.url ?? "/", "http://localhost");
    try {
      if (url.pathname === "/" && req.method === "GET") {
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        res.end(DASHBOARD_HTML);
        return;
      }
      if (url.pathname === "/api/status" && req.method === "GET") {
        this.sendJson(res, this.buildStatus());
        return;
      }
      if (url.pathname === "/api/portfolio" && req.method === "GET") {
        this.sendJson(res, await this.buildPortfolio());
        return;
      }
      if (url.pathname === "/api/trades" && req.method === "GET") {
        const limit = Math.min(200, Number(url.searchParams.get("limit") ?? "50") || 50);
        const beforeParam = url.searchParams.get("before");
        const before = beforeParam ? Number(beforeParam) : undefined;
        this.sendJson(res, this.store.readTrades(limit, before));
        return;
      }
      res.writeHead(404, { "Content-Type": "text/plain" });
      res.end("Not found");
    } catch (err) {
      this.logger.error("Dashboard request failed", { error: String(err), path: url.pathname });
      res.writeHead(500, { "Content-Type": "text/plain" });
      res.end("Internal error");
    }
  }

  private sendJson(res: ServerResponse, data: unknown): void {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify(data));
  }

  private buildStatus() {
    return {
      mode: this.config.trading.mode,
      environment: this.config.kalshi.environment,
      riskTripped: this.riskManager.isTripped(),
      uptimeSeconds: Math.floor((Date.now() - this.scanner.getStartedAt()) / 1000),
      trackedMarkets: this.scanner.getMarkets().size,
      trackedEvents: this.scanner.getEvents().size,
    };
  }

  private async buildPortfolio() {
    if (this.config.trading.mode === "live") {
      const [balance, positions] = await Promise.all([
        this.restClient.getBalance(),
        this.restClient.getPositions(),
      ]);
      return {
        source: "live" as const,
        cashCents: balance.balanceCents,
        portfolioValueCents: balance.portfolioValueCents,
        positions,
      };
    }

    const state = this.portfolio.getState();
    const markets: ReadonlyMap<string, Market> = this.scanner.getMarkets();
    return {
      source: "paper" as const,
      cashCents: state.cashCents,
      realizedPnlCents: state.realizedPnlCents,
      unrealizedPnlCents: this.portfolio.unrealizedPnlCents(markets),
      positions: Object.entries(state.positions).map(([ticker, position]) => ({
        ticker,
        title: markets.get(ticker)?.title ?? ticker,
        ...position,
      })),
    };
  }
}
