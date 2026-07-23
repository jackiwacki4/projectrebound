import { config } from "./config.js";
import { createLogger } from "./logger.js";
import { KalshiRestClient } from "./kalshi/restClient.js";
import { PaperPortfolio } from "./portfolio/paperPortfolio.js";
import { PortfolioStore } from "./portfolio/store.js";
import { RiskManager } from "./risk/riskManager.js";
import { Executor } from "./engine/executor.js";
import { ScannerEngine } from "./engine/scanner.js";
import { sameMarketArb } from "./strategies/sameMarketArb.js";
import { multiOutcomeArb } from "./strategies/multiOutcomeArb.js";
import { createNewsReactionStrategy } from "./strategies/newsReaction.js";

async function main(): Promise<void> {
  const logger = createLogger(config.logLevel);
  logger.info(
    `Starting projectrebound in ${config.trading.mode.toUpperCase()} mode against Kalshi ${config.kalshi.environment}`
  );
  if (config.trading.mode === "live") {
    logger.warn("LIVE TRADING IS ENABLED. Real orders will be submitted to Kalshi.");
  } else {
    logger.info("Paper mode: no real orders will be sent, fills are simulated locally.");
  }

  const restClient = new KalshiRestClient(config, logger);
  const store = new PortfolioStore("./data/portfolio.json", "./data/trades.jsonl");
  const portfolio = new PaperPortfolio(config.paper.startingCashCents, store, logger);
  const riskManager = new RiskManager(config, logger, portfolio);
  const executor = new Executor(config, logger, portfolio, restClient);

  const strategies = [sameMarketArb, multiOutcomeArb, createNewsReactionStrategy(logger)];

  const scanner = new ScannerEngine(config, logger, restClient, riskManager, executor, strategies);
  await scanner.start();

  const shutdown = () => {
    logger.info("Shutting down...");
    scanner.stop();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

main().catch((err) => {
  console.error("Fatal error starting projectrebound:", err);
  process.exit(1);
});
