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
}
