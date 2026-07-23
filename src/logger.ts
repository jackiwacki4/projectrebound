const levels = ["debug", "info", "warn", "error"] as const;
type Level = (typeof levels)[number];

function rank(level: Level): number {
  return levels.indexOf(level);
}

export function createLogger(minLevel: string) {
  const threshold = rank(levels.includes(minLevel as Level) ? (minLevel as Level) : "info");

  function log(level: Level, msg: string, meta?: Record<string, unknown>) {
    if (rank(level) < threshold) return;
    const line = `${new Date().toISOString()} [${level.toUpperCase()}] ${msg}`;
    const out = level === "error" ? console.error : console.log;
    out(meta ? `${line} ${JSON.stringify(meta)}` : line);
  }

  return {
    debug: (msg: string, meta?: Record<string, unknown>) => log("debug", msg, meta),
    info: (msg: string, meta?: Record<string, unknown>) => log("info", msg, meta),
    warn: (msg: string, meta?: Record<string, unknown>) => log("warn", msg, meta),
    error: (msg: string, meta?: Record<string, unknown>) => log("error", msg, meta),
  };
}

export type Logger = ReturnType<typeof createLogger>;
