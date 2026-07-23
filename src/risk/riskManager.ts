import type { Market, OrderIntent } from "../types.js";
import type { Logger } from "../logger.js";
import type { PaperPortfolio } from "../portfolio/paperPortfolio.js";
import type { Config } from "../config.js";

function startOfDayMs(): number {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/**
 * Clamps and filters strategy output before it reaches the executor:
 * per-market position caps, per-event exposure caps, an order-rate limiter,
 * and a same-day P&L kill switch (realized + mark-to-market unrealized).
 * Once tripped, the kill switch stays tripped until the next calendar day
 * or a process restart -- it will not silently resume same-day.
 *
 * Known limitation: the same-day P&L baseline is captured on first use
 * (construction / first admit() call), not at a fixed wall-clock day
 * boundary. A process restart mid-day resets that baseline to whatever
 * P&L already exists at that moment, effectively forgiving losses booked
 * earlier that day. Acceptable for a v1 running as one long-lived process;
 * revisit if the bot is expected to restart frequently during a trading day.
 */
export class RiskManager {
  private orderTimestamps: number[] = [];
  private dayStartMs = startOfDayMs();
  private pnlBaselineCents = 0;
  private baselineInitialized = false;
  private tripped = false;

  constructor(
    private readonly config: Config,
    private readonly logger: Logger,
    private readonly portfolio: PaperPortfolio
  ) {}

  admit(intents: OrderIntent[], markets: Map<string, Market>): OrderIntent[] {
    this.rolloverDayIfNeeded(markets);
    if (this.tripped) return [];

    const currentPnlCents =
      this.portfolio.getState().realizedPnlCents + this.portfolio.unrealizedPnlCents(markets);
    const dailyPnlCents = currentPnlCents - this.pnlBaselineCents;
    if (dailyPnlCents <= -this.config.risk.maxDailyLossCents) {
      this.tripped = true;
      this.logger.error("Daily loss limit hit -- trading halted until restart or next day", {
        dailyPnlCents,
      });
      return [];
    }

    const admitted: OrderIntent[] = [];
    for (const intent of intents) {
      if (!this.withinRateLimit()) {
        this.logger.warn("Order rate limit reached, dropping remaining signals this cycle");
        break;
      }

      const sized = this.clampToPositionCap(intent);
      if (!sized) continue;

      const eventTicker = markets.get(intent.ticker)?.eventTicker;
      if (eventTicker && !this.withinEventExposure(eventTicker, sized, markets)) {
        this.logger.warn("Event exposure cap reached, dropping signal", { eventTicker });
        continue;
      }

      admitted.push(sized);
      this.recordOrderTimestamp();
    }
    return admitted;
  }

  private clampToPositionCap(intent: OrderIntent): OrderIntent | undefined {
    const existingCents = this.portfolio.positionNotionalCents(intent.ticker);
    const roomCents = this.config.risk.maxPositionCents - existingCents;
    if (roomCents <= 0) return undefined;
    const maxCount = Math.floor(roomCents / intent.limitPriceCents);
    if (maxCount <= 0) return undefined;
    return { ...intent, count: Math.min(intent.count, maxCount) };
  }

  private withinEventExposure(
    eventTicker: string,
    intent: OrderIntent,
    markets: Map<string, Market>
  ): boolean {
    let exposureCents = 0;
    for (const [ticker, market] of markets) {
      if (market.eventTicker !== eventTicker) continue;
      exposureCents += this.portfolio.positionNotionalCents(ticker);
    }
    exposureCents += intent.count * intent.limitPriceCents;
    return exposureCents <= this.config.risk.maxEventExposureCents;
  }

  private withinRateLimit(): boolean {
    this.pruneOldTimestamps();
    return this.orderTimestamps.length < this.config.risk.maxOrdersPerMinute;
  }

  private recordOrderTimestamp(): void {
    this.orderTimestamps.push(Date.now());
  }

  private pruneOldTimestamps(): void {
    const cutoff = Date.now() - 60_000;
    this.orderTimestamps = this.orderTimestamps.filter((t) => t > cutoff);
  }

  private rolloverDayIfNeeded(markets: Map<string, Market>): void {
    const today = startOfDayMs();
    if (!this.baselineInitialized || today !== this.dayStartMs) {
      this.dayStartMs = today;
      this.pnlBaselineCents =
        this.portfolio.getState().realizedPnlCents + this.portfolio.unrealizedPnlCents(markets);
      this.baselineInitialized = true;
      this.tripped = false;
    }
  }
}
