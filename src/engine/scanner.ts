import { KalshiRestClient } from "../kalshi/restClient.js";
import { KalshiWebSocketClient } from "../kalshi/wsClient.js";
import { NewsFeedPoller } from "../news/newsFeed.js";
import type { RiskManager } from "../risk/riskManager.js";
import type { Executor } from "./executor.js";
import type { Config } from "../config.js";
import type { Logger } from "../logger.js";
import type { Strategy, StrategyContext } from "../strategies/types.js";
import type { EventInfo, Market, NewsItem, Orderbook } from "../types.js";

/** Wires market data, order books, and news into the strategies, then risk-checks and executes what they emit. */
export class ScannerEngine {
  private readonly markets = new Map<string, Market>();
  private readonly events = new Map<string, EventInfo>();
  private readonly orderbooks = new Map<string, Orderbook>();
  private pollTimer?: NodeJS.Timeout;
  private readonly ws: KalshiWebSocketClient;
  private readonly newsFeed: NewsFeedPoller;

  constructor(
    private readonly config: Config,
    private readonly logger: Logger,
    private readonly restClient: KalshiRestClient,
    private readonly riskManager: RiskManager,
    private readonly executor: Executor,
    private readonly strategies: Strategy[]
  ) {
    this.ws = new KalshiWebSocketClient(config, logger, (ob) => {
      this.orderbooks.set(ob.ticker, ob);
      void this.runMarketStrategies();
    });
    this.newsFeed = new NewsFeedPoller(
      config.scanning.newsFeeds,
      config.scanning.newsPollIntervalMs,
      logger,
      (item) => void this.runNewsStrategies(item)
    );
  }

  async start(): Promise<void> {
    await this.refreshMarkets();
    this.ws.connect();
    this.newsFeed.start();
    this.pollTimer = setInterval(
      () => void this.refreshMarkets(),
      this.config.scanning.marketPollIntervalMs
    );
  }

  stop(): void {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.ws.close();
    this.newsFeed.stop();
  }

  private buildContext(): StrategyContext {
    return {
      markets: this.markets,
      events: this.events,
      orderbooks: this.orderbooks,
      minArbEdgeCents: this.config.scanning.minArbEdgeCents,
    };
  }

  private async refreshMarkets(): Promise<void> {
    try {
      const [marketList, eventList] = await Promise.all([
        this.restClient.listOpenMarkets(),
        this.restClient.listEvents(),
      ]);
      for (const m of marketList) this.markets.set(m.ticker, m);
      for (const e of eventList) this.events.set(e.eventTicker, e);
      this.ws.subscribeMarkets(marketList.map((m) => m.ticker));
      this.logger.info(`Tracking ${this.markets.size} markets across ${this.events.size} events`);
      await this.runMarketStrategies();
    } catch (err) {
      this.logger.error("Failed to refresh markets", { error: String(err) });
    }
  }

  private async runMarketStrategies(): Promise<void> {
    const ctx = this.buildContext();
    for (const strategy of this.strategies) {
      if (!strategy.onMarketUpdate) continue;
      const intents = strategy.onMarketUpdate(ctx);
      if (intents.length === 0) continue;
      const admitted = this.riskManager.admit(intents, this.markets);
      if (admitted.length > 0) await this.executor.execute(admitted);
    }
  }

  private async runNewsStrategies(item: NewsItem): Promise<void> {
    const ctx = this.buildContext();
    for (const strategy of this.strategies) {
      if (!strategy.onNews) continue;
      const intents = strategy.onNews(item, ctx);
      if (intents.length === 0) continue;
      const admitted = this.riskManager.admit(intents, this.markets);
      if (admitted.length > 0) await this.executor.execute(admitted);
    }
  }
}
