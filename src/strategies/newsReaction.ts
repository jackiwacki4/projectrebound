import type { NewsItem } from "../types.js";
import type { Strategy, StrategyContext } from "./types.js";
import type { Logger } from "../logger.js";

const STOPWORDS = new Set([
  "about", "after", "again", "against", "before", "could", "first", "should",
  "their", "there", "these", "those", "which", "while", "would", "years",
  "trump", "biden", "president", "government", "national", "official",
]);
const MIN_TOKEN_LENGTH = 4;
const MIN_MATCHING_TOKENS = 2;

function significantTokens(text: string): Set<string> {
  return new Set(
    text
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, " ")
      .split(/\s+/)
      .filter((t) => t.length >= MIN_TOKEN_LENGTH && !STOPWORDS.has(t))
  );
}

/**
 * Flags markets that a breaking-news headline might affect, by token overlap
 * with the market's title/subtitle. Deliberately does NOT emit OrderIntents:
 * keyword overlap tells you a market might be relevant, not which direction
 * (YES or NO) the news pushes it. Auto-trading on that without real
 * direction/sentiment analysis is a coin flip with fees attached, not an
 * edge -- so this is wired as a "go look now" alert, logged loudly, rather
 * than a signal into the executor. Swap in real sentiment/impact scoring
 * before ever turning this into live orders.
 */
export function createNewsReactionStrategy(logger: Logger): Strategy {
  return {
    name: "news-reaction-alert",
    onNews(item: NewsItem, ctx: StrategyContext) {
      const headlineTokens = significantTokens(item.headline);
      if (headlineTokens.size === 0) return [];

      for (const market of ctx.markets.values()) {
        if (market.status !== "open") continue;
        const marketTokens = significantTokens(`${market.title} ${market.subtitle ?? ""}`);
        const overlap = [...headlineTokens].filter((t) => marketTokens.has(t));
        if (overlap.length >= MIN_MATCHING_TOKENS) {
          logger.warn("News may affect market -- review manually", {
            ticker: market.ticker,
            title: market.title,
            headline: item.headline,
            url: item.url,
            matchedTerms: overlap,
          });
        }
      }

      return [];
    },
  };
}
