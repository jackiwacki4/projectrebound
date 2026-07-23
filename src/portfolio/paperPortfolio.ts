import type { Fill, Market } from "../types.js";
import type { Logger } from "../logger.js";
import { PortfolioStore } from "./store.js";

export interface Position {
  yesCount: number;
  yesCostCents: number;
  noCount: number;
  noCostCents: number;
}

export interface PortfolioState {
  cashCents: number;
  positions: Record<string, Position>;
  realizedPnlCents: number;
}

function emptyPosition(): Position {
  return { yesCount: 0, yesCostCents: 0, noCount: 0, noCostCents: 0 };
}

/**
 * Simulated portfolio. Every "fill" here is invented locally (see
 * engine/executor.ts) -- no order is ever sent to Kalshi in paper mode.
 */
export class PaperPortfolio {
  private state: PortfolioState;

  constructor(
    startingCashCents: number,
    private readonly store: PortfolioStore,
    private readonly logger: Logger
  ) {
    this.state = this.store.load() ?? {
      cashCents: startingCashCents,
      positions: {},
      realizedPnlCents: 0,
    };
  }

  getState(): Readonly<PortfolioState> {
    return this.state;
  }

  getPosition(ticker: string): Readonly<Position> {
    return this.state.positions[ticker] ?? emptyPosition();
  }

  positionNotionalCents(ticker: string): number {
    const p = this.getPosition(ticker);
    return p.yesCostCents + p.noCostCents;
  }

  /** Mark-to-market unrealized P&L, valuing open positions at the current best bid. */
  unrealizedPnlCents(markets: Map<string, Market>): number {
    let total = 0;
    for (const [ticker, position] of Object.entries(this.state.positions)) {
      const market = markets.get(ticker);
      if (!market) continue;
      if (position.yesCount > 0) {
        total += position.yesCount * market.yesBidCents - position.yesCostCents;
      }
      if (position.noCount > 0) {
        total += position.noCount * market.noBidCents - position.noCostCents;
      }
    }
    return total;
  }

  applyFill(fill: Fill): void {
    const position = this.state.positions[fill.ticker] ?? emptyPosition();
    const totalCents = fill.count * fill.priceCents;
    const countField = fill.side === "yes" ? "yesCount" : "noCount";
    const costField = fill.side === "yes" ? "yesCostCents" : "noCostCents";

    if (fill.action === "buy") {
      this.state.cashCents -= totalCents;
      position[countField] += fill.count;
      position[costField] += totalCents;
    } else {
      this.state.cashCents += totalCents;
      const avgCostCents = position[countField] > 0 ? position[costField] / position[countField] : 0;
      this.state.realizedPnlCents += totalCents - avgCostCents * fill.count;
      position[countField] -= fill.count;
      position[costField] -= avgCostCents * fill.count;
    }

    this.state.positions[fill.ticker] = position;
    this.store.appendTrade(fill);
    this.store.save(this.state);
    this.logger.info("Paper fill applied", {
      ticker: fill.ticker,
      side: fill.side,
      action: fill.action,
      count: fill.count,
      priceCents: fill.priceCents,
      strategy: fill.strategy,
      cashCents: this.state.cashCents,
    });
  }
}
