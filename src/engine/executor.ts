import { randomUUID } from "node:crypto";
import type { Fill, OrderIntent } from "../types.js";
import type { PaperPortfolio } from "../portfolio/paperPortfolio.js";
import type { KalshiRestClient } from "../kalshi/restClient.js";
import type { Config } from "../config.js";
import type { Logger } from "../logger.js";

/** Dispatches admitted order intents to either the paper portfolio or the real Kalshi API. */
export class Executor {
  constructor(
    private readonly config: Config,
    private readonly logger: Logger,
    private readonly portfolio: PaperPortfolio,
    private readonly restClient: KalshiRestClient
  ) {}

  async execute(intents: OrderIntent[]): Promise<void> {
    for (const intent of intents) {
      if (this.config.trading.mode === "paper") {
        this.applyPaperFill(intent);
      } else {
        await this.placeLiveOrder(intent);
      }
    }
  }

  private applyPaperFill(intent: OrderIntent): void {
    const fill: Fill = {
      ticker: intent.ticker,
      side: intent.side,
      action: intent.action,
      count: intent.count,
      priceCents: intent.limitPriceCents,
      strategy: intent.strategy,
      reason: intent.reason,
      ts: Date.now(),
    };
    this.portfolio.applyFill(fill);
  }

  private async placeLiveOrder(intent: OrderIntent): Promise<void> {
    try {
      const { orderId } = await this.restClient.createOrder({
        ticker: intent.ticker,
        side: intent.side,
        action: intent.action,
        count: intent.count,
        limitPriceCents: intent.limitPriceCents,
        clientOrderId: randomUUID(),
      });
      this.logger.info("Live order placed", {
        orderId,
        ticker: intent.ticker,
        side: intent.side,
        count: intent.count,
        limitPriceCents: intent.limitPriceCents,
        strategy: intent.strategy,
      });
    } catch (err) {
      this.logger.error("Live order failed", { error: String(err), ticker: intent.ticker });
    }
  }
}
