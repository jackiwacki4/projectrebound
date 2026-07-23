import Parser from "rss-parser";
import type { NewsItem } from "../types.js";
import type { Logger } from "../logger.js";

export class NewsFeedPoller {
  private readonly parser = new Parser();
  private readonly seen = new Set<string>();
  private readonly primedFeeds = new Set<string>();
  private timer?: NodeJS.Timeout;

  constructor(
    private readonly feedUrls: string[],
    private readonly pollIntervalMs: number,
    private readonly logger: Logger,
    private readonly onItem: (item: NewsItem) => void
  ) {}

  start(): void {
    if (this.feedUrls.length === 0) {
      this.logger.warn("No NEWS_FEEDS configured; news-reaction strategy is inactive.");
      return;
    }
    void this.pollOnce();
    this.timer = setInterval(() => void this.pollOnce(), this.pollIntervalMs);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
  }

  private async pollOnce(): Promise<void> {
    for (const url of this.feedUrls) {
      try {
        const feed = await this.parser.parseURL(url);
        // First pass per feed just establishes a baseline so we don't treat
        // a whole feed backlog as breaking news on startup.
        const isPrimingPass = !this.primedFeeds.has(url);

        for (const entry of feed.items ?? []) {
          const key = entry.link ?? entry.guid ?? entry.title ?? "";
          if (!key || this.seen.has(key)) continue;
          this.seen.add(key);
          if (isPrimingPass) continue;

          this.onItem({
            source: feed.title ?? url,
            headline: entry.title ?? "",
            url: entry.link ?? "",
            publishedAt: entry.isoDate ?? entry.pubDate ?? new Date().toISOString(),
          });
        }
        this.primedFeeds.add(url);
      } catch (err) {
        this.logger.warn(`Failed to poll news feed ${url}`, { error: String(err) });
      }
    }
  }
}
