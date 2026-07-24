export const DASHBOARD_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>projectrebound</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0b0d12;
    --panel: #131722;
    --panel-border: #232838;
    --text: #e6e9f0;
    --muted: #8892a6;
    --green: #3ecf8e;
    --red: #f2545b;
    --amber: #e8a33d;
    --accent: #5b8def;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 24px;
  }
  .mono { font-variant-numeric: tabular-nums; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    margin-bottom: 24px;
  }
  h1 { font-size: 20px; margin: 0; letter-spacing: 0.02em; }
  .badges { display: flex; gap: 8px; flex-wrap: wrap; }
  .badge {
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid var(--panel-border);
    background: var(--panel);
    color: var(--muted);
  }
  .badge.ok { color: var(--green); border-color: color-mix(in srgb, var(--green) 40%, var(--panel-border)); }
  .badge.warn { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 40%, var(--panel-border)); }
  .badge.bad { color: var(--red); border-color: color-mix(in srgb, var(--red) 40%, var(--panel-border)); }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 10px;
    padding: 16px;
  }
  .card .label { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .card .value { font-size: 22px; font-weight: 600; }
  .value.pos { color: var(--green); }
  .value.neg { color: var(--red); }
  section { margin-bottom: 28px; }
  section h2 { font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 10px; }
  table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--panel-border); border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: 10px 12px; font-size: 13px; border-bottom: 1px solid var(--panel-border); }
  th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
  tr:last-child td { border-bottom: none; }
  .side-yes { color: var(--green); }
  .side-no { color: var(--red); }
  .action-buy { color: var(--red); } /* money out */
  .action-sell { color: var(--green); } /* money in */
  .muted { color: var(--muted); }
  #loadMore { margin-top: 10px; background: var(--panel); color: var(--text); border: 1px solid var(--panel-border); padding: 8px 14px; border-radius: 8px; cursor: pointer; }
  #loadMore:hover { border-color: var(--accent); }
  .empty { color: var(--muted); padding: 16px; text-align: center; }
</style>
</head>
<body>
  <header>
    <h1>projectrebound</h1>
    <div class="badges" id="statusBadges"></div>
  </header>

  <div class="cards" id="summaryCards"></div>

  <section>
    <h2>Open positions</h2>
    <div id="positionsWrap"></div>
  </section>

  <section>
    <h2>Trade ledger</h2>
    <div id="tradesWrap"></div>
    <button id="loadMore" style="display:none">Load older trades</button>
  </section>

  <script>
    const fmtUsd = (cents) => {
      const sign = cents < 0 ? "-" : "";
      return sign + "$" + (Math.abs(cents) / 100).toFixed(2);
    };
    const fmtPnl = (cents) => (cents >= 0 ? "+" : "") + fmtUsd(cents);
    const pnlClass = (cents) => (cents > 0 ? "pos" : cents < 0 ? "neg" : "");
    const fmtTime = (ts) => new Date(ts).toLocaleString();

    let oldestLoadedTs = undefined;

    async function fetchJson(path) {
      const res = await fetch(path);
      if (!res.ok) throw new Error("request failed: " + res.status);
      return res.json();
    }

    function renderStatus(status) {
      const el = document.getElementById("statusBadges");
      const modeClass = status.mode === "live" ? "bad" : "ok";
      const riskClass = status.riskTripped ? "bad" : "ok";
      const hrs = Math.floor(status.uptimeSeconds / 3600);
      const mins = Math.floor((status.uptimeSeconds % 3600) / 60);
      el.innerHTML = \`
        <span class="badge \${modeClass}">\${status.mode.toUpperCase()} / \${status.environment}</span>
        <span class="badge \${riskClass}">\${status.riskTripped ? "KILL SWITCH TRIPPED" : "risk ok"}</span>
        <span class="badge">\${status.trackedMarkets} markets / \${status.trackedEvents} events</span>
        <span class="badge">up \${hrs}h \${mins}m</span>
      \`;
    }

    function renderPortfolio(p) {
      const cards = document.getElementById("summaryCards");
      const netWorth = p.cashCents + (p.portfolioValueCents ?? 0);
      const items = [
        { label: "Cash", value: fmtUsd(p.cashCents) },
      ];
      if (p.source === "paper") {
        items.push({ label: "Realized P&L", value: fmtPnl(p.realizedPnlCents), cls: pnlClass(p.realizedPnlCents) });
        items.push({ label: "Unrealized P&L", value: fmtPnl(p.unrealizedPnlCents), cls: pnlClass(p.unrealizedPnlCents) });
        items.push({ label: "Net worth", value: fmtUsd(p.cashCents + p.unrealizedPnlCents) });
      } else {
        items.push({ label: "Portfolio value", value: fmtUsd(p.portfolioValueCents) });
        items.push({ label: "Net worth", value: fmtUsd(netWorth) });
      }
      cards.innerHTML = items
        .map((i) => \`<div class="card"><div class="label">\${i.label}</div><div class="value mono \${i.cls ?? ""}">\${i.value}</div></div>\`)
        .join("");

      const wrap = document.getElementById("positionsWrap");
      if (!p.positions || p.positions.length === 0) {
        wrap.innerHTML = '<div class="empty">No open positions.</div>';
        return;
      }
      const isPaper = p.source === "paper";
      const rows = p.positions
        .filter((pos) => (pos.yesCount ?? pos.positionCount ?? 0) !== 0 || (pos.noCount ?? 0) !== 0)
        .map((pos) => {
          if (isPaper) {
            return \`<tr>
              <td>\${pos.title ?? pos.ticker}</td>
              <td class="mono">\${pos.ticker}</td>
              <td class="side-yes mono">\${pos.yesCount} @ \${pos.yesCount ? fmtUsd(pos.yesCostCents / pos.yesCount) : "-"}</td>
              <td class="side-no mono">\${pos.noCount} @ \${pos.noCount ? fmtUsd(pos.noCostCents / pos.noCount) : "-"}</td>
            </tr>\`;
          }
          return \`<tr>
            <td class="mono">\${pos.ticker}</td>
            <td class="mono">\${pos.positionCount}</td>
            <td class="mono">\${fmtUsd(pos.marketExposureCents)}</td>
            <td class="mono \${pnlClass(pos.realizedPnlCents)}">\${fmtPnl(pos.realizedPnlCents)}</td>
          </tr>\`;
        })
        .join("");
      const header = p.source === "paper"
        ? "<tr><th>Market</th><th>Ticker</th><th>YES</th><th>NO</th></tr>"
        : "<tr><th>Ticker</th><th>Net contracts</th><th>Exposure</th><th>Realized P&L</th></tr>";
      wrap.innerHTML = rows
        ? \`<table><thead>\${header}</thead><tbody>\${rows}</tbody></table>\`
        : '<div class="empty">No open positions.</div>';
    }

    function tradeRow(t) {
      return \`<tr>
        <td class="muted">\${fmtTime(t.ts)}</td>
        <td class="mono">\${t.ticker}</td>
        <td class="action-\${t.action}">\${t.action.toUpperCase()}</td>
        <td class="side-\${t.side}">\${t.side.toUpperCase()}</td>
        <td class="mono">\${t.count}</td>
        <td class="mono">\${fmtUsd(t.priceCents)}</td>
        <td class="mono \${t.action === 'buy' ? 'action-buy' : 'action-sell'}">\${t.action === 'buy' ? '-' : '+'}\${fmtUsd(t.count * t.priceCents)}</td>
        <td class="muted">\${t.strategy}</td>
      </tr>\`;
    }

    async function loadTrades(before) {
      const qs = before ? "?limit=50&before=" + before : "?limit=50";
      const trades = await fetchJson("/api/trades" + qs);
      const wrap = document.getElementById("tradesWrap");
      const loadMore = document.getElementById("loadMore");
      if (trades.length === 0 && !before) {
        wrap.innerHTML = '<div class="empty">No trades yet.</div>';
        loadMore.style.display = "none";
        return;
      }
      const rowsHtml = trades.map(tradeRow).join("");
      if (before) {
        document.getElementById("tradesTbody").insertAdjacentHTML("beforeend", rowsHtml);
      } else {
        wrap.innerHTML = \`<table><thead><tr>
          <th>Time</th><th>Ticker</th><th>Action</th><th>Side</th><th>Count</th><th>Price</th><th>Cash flow</th><th>Strategy</th>
        </tr></thead><tbody id="tradesTbody">\${rowsHtml}</tbody></table>\`;
      }
      if (trades.length > 0) {
        oldestLoadedTs = trades[trades.length - 1].ts;
        loadMore.style.display = trades.length < 50 ? "none" : "inline-block";
      } else {
        loadMore.style.display = "none";
      }
    }

    document.getElementById("loadMore").addEventListener("click", () => loadTrades(oldestLoadedTs));

    async function refresh() {
      try {
        const [status, portfolio] = await Promise.all([
          fetchJson("/api/status"),
          fetchJson("/api/portfolio"),
        ]);
        renderStatus(status);
        renderPortfolio(portfolio);
      } catch (err) {
        console.error(err);
      }
    }

    refresh();
    loadTrades();
    setInterval(refresh, 10000);
  </script>
</body>
</html>
`;
