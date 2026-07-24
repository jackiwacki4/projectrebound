import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname } from "node:path";
import type { Fill } from "../types.js";
import type { PortfolioState } from "./paperPortfolio.js";

export class PortfolioStore {
  constructor(
    private readonly portfolioPath: string,
    private readonly tradesLogPath: string
  ) {
    for (const p of [portfolioPath, tradesLogPath]) {
      const dir = dirname(p);
      if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    }
  }

  load(): PortfolioState | undefined {
    if (!existsSync(this.portfolioPath)) return undefined;
    try {
      return JSON.parse(readFileSync(this.portfolioPath, "utf8")) as PortfolioState;
    } catch {
      return undefined;
    }
  }

  save(state: PortfolioState): void {
    const tmpPath = `${this.portfolioPath}.tmp`;
    writeFileSync(tmpPath, JSON.stringify(state, null, 2));
    renameSync(tmpPath, this.portfolioPath);
  }

  appendTrade(fill: Fill): void {
    appendFileSync(this.tradesLogPath, `${JSON.stringify(fill)}\n`);
  }

  /** Most-recent-first trade ledger, for the dashboard. `beforeTs` pages backward in time. */
  readTrades(limit: number, beforeTs?: number): Fill[] {
    if (!existsSync(this.tradesLogPath)) return [];
    const lines = readFileSync(this.tradesLogPath, "utf8").split("\n").filter(Boolean);
    const fills: Fill[] = [];
    for (const line of lines) {
      try {
        fills.push(JSON.parse(line) as Fill);
      } catch {
        // skip a malformed/partial line rather than fail the whole read
      }
    }
    fills.reverse();
    const filtered = beforeTs === undefined ? fills : fills.filter((f) => f.ts < beforeTs);
    return filtered.slice(0, limit);
  }
}
