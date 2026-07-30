"""Self-contained HTML dashboard.

Writes ONE file with everything inlined -- no server, no port, no login, no
network. You open it in a browser like any document. That is deliberate: a
listening web server on a machine holding trading credentials is a security
surface with no upside here, and the CLI-only rule in the README exists for that
reason. A static file keeps the visual without the exposure.

Charts are hand-built SVG. Colour follows the project's data-viz rules:

  * Every chart plots ONE series, so it uses one validated categorical slot
    (blue) and needs no legend -- the title names the series.
  * Profit and loss are NEVER colour alone. The validator scores status-green
    against status-red at CVD deltaE 4.1 (deuteranopia), i.e. indistinguishable
    for red-green colourblind readers, so every signed number also carries an
    arrow glyph and a word.
  * The grid and axes are recessive; the reference diagonal on the calibration
    chart is a dashed neutral, not a second series.
  * Every chart has a hover tooltip, and the entry table doubles as the
    non-visual view of the same numbers.
"""
from __future__ import annotations

import html
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..execution.fees import taker_fee_cents
from .calibration import MIN_SAMPLE, _won

# --- palette: validated reference instance, only the roles this page uses ---
_CSS = """
:root {
  color-scheme: light;
  --surface-0: #f4f3f0; --surface-1: #fcfcfb; --border: #e2e1dc;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #77766f;
  --series-1: #2a78d6; --grid: #e8e7e2;
  --good: #0ca30c; --critical: #d03b3b; --warning: #fab219;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface-0: #111110; --surface-1: #1a1a19; --border: #33332f;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e85;
    --series-1: #3987e5; --grid: #2b2b28;
    --good: #0ca30c; --critical: #d03b3b; --warning: #fab219;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 16px 64px; background: var(--surface-0);
  color: var(--text-primary);
  font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 980px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 2px; letter-spacing: -0.01em; }
h2 { font-size: 15px; margin: 0 0 4px; }
.sub { color: var(--text-secondary); font-size: 13px; margin: 0 0 20px; }
.card {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px; margin-bottom: 14px;
}
.card > p.note { color: var(--text-muted); font-size: 12.5px; margin: 2px 0 12px; }
.tiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }
.tile { background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 10px; padding: 14px 16px; }
.tile .label { color: var(--text-secondary); font-size: 12px; text-transform: uppercase;
               letter-spacing: 0.04em; }
.tile .value { font-size: 26px; font-weight: 600; margin-top: 4px; letter-spacing: -0.02em; }
.tile .foot { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
.banner { border-left: 3px solid var(--warning); background: var(--surface-1);
          border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; font-size: 13.5px; }
.banner strong { display: block; margin-bottom: 3px; }
.pos { color: var(--good); } .neg { color: var(--critical); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--text-secondary); font-weight: 600;
     border-bottom: 1px solid var(--border); padding: 6px 8px; white-space: nowrap; }
td { padding: 6px 8px; border-bottom: 1px solid var(--grid); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }
svg { display: block; max-width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--text-secondary); font-size: 11px; }
svg .axis { stroke: var(--border); stroke-width: 1; }
svg .grid { stroke: var(--grid); stroke-width: 1; }
svg .refline { stroke: var(--text-muted); stroke-width: 1.5; stroke-dasharray: 4 4; opacity: 0.7; }
svg .line { fill: none; stroke: var(--series-1); stroke-width: 2; }
svg .dot { fill: var(--series-1); stroke: var(--surface-1); stroke-width: 2; }
svg .bar { fill: var(--series-1); }
svg .hit { fill: transparent; cursor: default; }
#tip { position: fixed; z-index: 9; pointer-events: none; opacity: 0;
       transition: opacity .08s; background: var(--text-primary);
       color: var(--surface-1); font-size: 12px; line-height: 1.4;
       padding: 6px 9px; border-radius: 6px; max-width: 260px; }
footer { color: var(--text-muted); font-size: 12px; margin-top: 26px; }
footer li { margin-bottom: 4px; }
"""

_JS = """
(function () {
  var tip = document.getElementById('tip');
  document.addEventListener('mouseover', function (e) {
    var t = e.target.closest('[data-tip]');
    if (!t) return;
    tip.textContent = t.getAttribute('data-tip');
    tip.style.opacity = '1';
  });
  document.addEventListener('mousemove', function (e) {
    if (tip.style.opacity !== '1') return;
    var pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    var x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > innerWidth) x = e.clientX - w - pad;
    if (y + h > innerHeight) y = e.clientY - h - pad;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  });
  document.addEventListener('mouseout', function (e) {
    if (e.target.closest('[data-tip]')) tip.style.opacity = '0';
  });
})();
"""


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents)/100:,.2f}"


def _signed(cents: int) -> str:
    """Signed money with a glyph and a word, so the sign never rides on colour
    alone -- status green and status red are CVD-indistinguishable (deltaE 4.1)."""
    if cents > 0:
        return f'<span class="pos">&#9650; up {_usd(cents)}</span>'
    if cents < 0:
        return f'<span class="neg">&#9660; down {_usd(abs(cents))}</span>'
    return '<span>flat $0.00</span>'


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
def _collection_status(conn, db_path: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) n, MIN(captured_ts) lo, MAX(captured_ts) hi FROM book_snapshots"
    ).fetchone()
    now = datetime.now(tz=timezone.utc).timestamp() * 1000
    size = Path(db_path).stat().st_size / 1_048_576 if Path(db_path).exists() else 0.0
    if not row["n"]:
        return {"snapshots": 0, "hours": 0.0, "minutes_ago": None, "size_mb": size}
    return {"snapshots": row["n"], "hours": (row["hi"] - row["lo"]) / 3_600_000,
            "minutes_ago": (now - row["hi"]) / 60_000, "size_mb": size}


def _entries(dao, risk_cfg=None) -> list:
    """One entry per settled market, chosen on the DECISION-QUALITY gates.

    Not on `gate_passed`: the account-state gates (funded balance, exposure cap)
    reject everything while the account holds $0, which on live data rejected all
    5,453 otherwise-qualifying candidates and made this page permanently empty.
    Shares its selection rule with the sweep and the text report."""
    from .sweep import first_qualifying_per_market, load_candidates, quality_thresholds
    entries = first_qualifying_per_market(load_candidates(dao),
                                          **quality_thresholds(risk_cfg))
    return sorted(entries, key=lambda c: c.ts)


def _calibration(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT d.probability p, s.result r FROM decisions d "
        "JOIN settlements s ON s.ticker = d.ticker"
    ).fetchall()
    buckets: dict[int, list[tuple[float, int]]] = {i: [] for i in range(10)}
    for r in rows:
        p = float(r["p"])
        buckets[min(9, int(p * 10))].append((p, 1 if r["r"] == "yes" else 0))
    out = []
    for b, items in buckets.items():
        if not items:
            continue
        out.append({"decile": b, "n": len(items),
                    "pred": 100 * sum(p for p, _ in items) / len(items),
                    "obs": 100 * sum(y for _, y in items) / len(items)})
    return out


def _brier_by_window(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.probability p, d.decision_ts dts, m.close_ts ct, s.result r
        FROM decisions d
        JOIN settlements s ON s.ticker = d.ticker
        JOIN markets m ON m.ticker = d.ticker
        WHERE m.close_ts IS NOT NULL
        """
    ).fetchall()
    windows = [(">24h", 24, 1e9), ("6-24h", 6, 24), ("2-6h", 2, 6), ("<2h", 0, 2)]
    out = []
    for label, lo, hi in windows:
        items = [r for r in rows if lo <= (r["ct"] - r["dts"]) / 3_600_000 < hi]
        if not items:
            continue
        brier = sum((float(r["p"]) - (1 if r["r"] == "yes" else 0)) ** 2
                    for r in items) / len(items)
        out.append({"label": label, "n": len(items), "brier": brier})
    return out


def _blocks(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT blocked_by, COUNT(*) c FROM decisions "
        "WHERE gate_passed = 0 AND blocked_by IS NOT NULL "
        "GROUP BY blocked_by ORDER BY c DESC"
    ).fetchall()


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------
def _calibration_svg(points: list[dict]) -> str:
    if not points:
        return '<p class="note">No settled decisions yet.</p>'
    # TOP reserves a band for the y-axis title. Without it the title sat on top
    # of the 100% tick label -- caught by rendering the page, not by reading it.
    W, H, P, TOP = 520, 318, 44, 30
    px = lambda v: P + (v / 100) * (W - P - 16)
    py = lambda v: (H - P) - (v / 100) * (H - P - TOP)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="Calibration: predicted probability versus observed frequency">']
    parts.append('<text x="6" y="14">what actually happened &#8593;</text>')
    for t in (0, 25, 50, 75, 100):
        parts.append(f'<line class="grid" x1="{P}" y1="{py(t):.1f}" x2="{W-16}" y2="{py(t):.1f}"/>')
        parts.append(f'<text x="{P-8}" y="{py(t)+4:.1f}" text-anchor="end">{t}%</text>')
        parts.append(f'<text x="{px(t):.1f}" y="{H-P+18}" text-anchor="middle">{t}%</text>')
    # The reference diagonal is not a series: dashed neutral, no legend entry.
    parts.append(f'<line class="refline" x1="{px(0):.1f}" y1="{py(0):.1f}" '
                 f'x2="{px(100):.1f}" y2="{py(100):.1f}"/>')
    # Labelled along the diagonal rather than at the corner, where it collided
    # with the top gridline and the 100% tick.
    parts.append(f'<text x="{px(72):.1f}" y="{py(72)+16:.1f}">perfect</text>')
    parts.append(f'<line class="axis" x1="{P}" y1="{H-P}" x2="{W-16}" y2="{H-P}"/>')
    parts.append(f'<line class="axis" x1="{P}" y1="{TOP-8}" x2="{P}" y2="{H-P}"/>')
    d = " ".join(f"{'M' if i == 0 else 'L'}{px(p['pred']):.1f},{py(p['obs']):.1f}"
                 for i, p in enumerate(points))
    parts.append(f'<path class="line" d="{d}"/>')
    for p in points:
        tip = (f"model said {p['pred']:.0f}% - actually happened {p['obs']:.0f}% "
               f"({p['n']:,} decision rows)")
        parts.append(f'<circle class="dot" cx="{px(p["pred"]):.1f}" cy="{py(p["obs"]):.1f}" '
                     f'r="5" data-tip="{_esc(tip)}"/>')
        parts.append(f'<circle class="hit" cx="{px(p["pred"]):.1f}" cy="{py(p["obs"]):.1f}" '
                     f'r="14" data-tip="{_esc(tip)}"/>')
    parts.append(f'<text x="{(W+P)/2:.0f}" y="{H-6}" text-anchor="middle">'
                 f'what the model predicted &#8594;</text>')
    parts.append("</svg>")
    return "".join(parts)


def _brier_svg(rows: list[dict]) -> str:
    if not rows:
        return '<p class="note">Needs settled markets with a known close time.</p>'
    W, rowh, P = 520, 34, 92
    H = len(rows) * rowh + 34
    worst = max(r["brier"] for r in rows) or 1.0
    scale = (W - P - 70) / max(worst, 0.30)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="Brier score by how long before settlement the call was made">']
    for i, r in enumerate(rows):
        y = i * rowh + 8
        w = max(2.0, r["brier"] * scale)
        tip = (f"{r['label']} before settlement: Brier {r['brier']:.4f} "
               f"over {r['n']:,} decision rows - lower is sharper")
        parts.append(f'<text x="{P-10}" y="{y+15}" text-anchor="end">{_esc(r["label"])}</text>')
        # 4px rounded data-end, anchored to the baseline at x=P.
        parts.append(f'<rect class="bar" x="{P}" y="{y}" width="{w:.1f}" height="20" '
                     f'rx="4" data-tip="{_esc(tip)}"/>')
        parts.append(f'<rect class="hit" x="{P}" y="{y-4}" width="{W-P}" height="28" '
                     f'data-tip="{_esc(tip)}"/>')
        parts.append(f'<text x="{P+w+8:.1f}" y="{y+15}">{r["brier"]:.3f}</text>')
    parts.append(f'<line class="axis" x1="{P}" y1="4" x2="{P}" y2="{H-26}"/>')
    parts.append(f'<text x="{P}" y="{H-6}">0.25 = no better than always saying 50/50; '
                 f'lower is sharper</text>')
    parts.append("</svg>")
    return "".join(parts)


def _pnl_svg(entries: list[sqlite3.Row]) -> str:
    if not entries:
        return ('<p class="note">No market met the minimum-edge bar yet, so there is '
                'nothing to plot. With no edge above the minimum, zero trades is the '
                'correct answer -- not a malfunction.</p>')
    running, cum = 0, []
    for e in entries:
        fee = taker_fee_cents(e.price, 1)
        running += ((100 - e.price) if _won(e.side, e.result) else -e.price) - fee
        cum.append((e.ts, running, e))
    W, H, P = 520, 260, 52
    lo = min(0, min(v for _, v, _ in cum))
    hi = max(0, max(v for _, v, _ in cum))
    span = (hi - lo) or 1
    n = len(cum)
    px = lambda i: P + (i / max(1, n - 1)) * (W - P - 16)
    py = lambda v: H - 34 - ((v - lo) / span) * (H - 58)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="Cumulative profit and loss of gate-passing entries, 1 contract each">']
    parts.append(f'<line class="grid" x1="{P}" y1="{py(0):.1f}" x2="{W-16}" y2="{py(0):.1f}"/>')
    parts.append(f'<text x="{P-8}" y="{py(0)+4:.1f}" text-anchor="end">$0</text>')
    for v in (lo, hi):
        if v:
            parts.append(f'<line class="grid" x1="{P}" y1="{py(v):.1f}" '
                         f'x2="{W-16}" y2="{py(v):.1f}"/>')
            parts.append(f'<text x="{P-8}" y="{py(v)+4:.1f}" text-anchor="end">'
                         f'{_usd(int(v))}</text>')
    d = " ".join(f"{'M' if i == 0 else 'L'}{px(i):.1f},{py(v):.1f}"
                 for i, (_, v, _) in enumerate(cum))
    parts.append(f'<path class="line" d="{d}"/>')
    for i, (ts, v, e) in enumerate(cum):
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%b %d %H:%M")
        tip = (f"{e.ticker} - {e.side.upper()} at {e.price}c, "
               f"{'won' if _won(e.side, e.result) else 'lost'} - "
               f"running total {_usd(int(v))} ({when} UTC)")
        parts.append(f'<circle class="dot" cx="{px(i):.1f}" cy="{py(v):.1f}" r="4.5" '
                     f'data-tip="{_esc(tip)}"/>')
        parts.append(f'<circle class="hit" cx="{px(i):.1f}" cy="{py(v):.1f}" r="15" '
                     f'data-tip="{_esc(tip)}"/>')
    parts.append(f'<text x="{P}" y="{H-8}">entry 1</text>')
    parts.append(f'<text x="{W-16}" y="{H-8}" text-anchor="end">entry {n}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------
def generate_dashboard(dao, family: str, db_path: str, stake_dollars: float = 10.0,
                       risk_cfg=None) -> str:
    conn = dao.conn
    status = _collection_status(conn, db_path)
    entries = _entries(dao, risk_cfg)
    calib = _calibration(conn)
    brier = _brier_by_window(conn)
    blocks = _blocks(conn)

    markets = conn.execute(
        "SELECT COUNT(DISTINCT d.ticker) c FROM decisions d "
        "JOIN settlements s ON s.ticker = d.ticker").fetchone()["c"]
    rows_behind = conn.execute(
        "SELECT COUNT(*) c FROM decisions d "
        "JOIN settlements s ON s.ticker = d.ticker").fetchone()["c"]

    pnl_1c = wins = 0
    for e in entries:
        fee = taker_fee_cents(e.price, 1)
        won = _won(e.side, e.result)
        wins += 1 if won else 0
        pnl_1c += ((100 - e.price) if won else -e.price) - fee

    stale = status["minutes_ago"] is not None and status["minutes_ago"] > 10
    o = []
    o.append('<div class="wrap">')
    o.append(f'<h1>{_esc(family.upper())} bot</h1>')
    if status["snapshots"]:
        live = ("collecting" if not stale else "LOOKS STOPPED")
        o.append(f'<p class="sub">{live} &middot; {status["hours"]:.1f} hours of price '
                 f'history &middot; last update {status["minutes_ago"]:.0f} min ago '
                 f'&middot; {status["size_mb"]:.0f} MB</p>')
    else:
        o.append('<p class="sub">no price history yet</p>')

    if stale:
        o.append('<div class="banner"><strong>&#9888; No new data for over 10 minutes.</strong>'
                 'The collector may have stopped. Check it with <code>./status.sh</code>.</div>')
    if markets < MIN_SAMPLE:
        o.append(f'<div class="banner"><strong>&#9888; Too early to conclude anything '
                 f'({markets} settled markets, want {MIN_SAMPLE}+).</strong>'
                 f'Everything below is for checking the machinery works. The '
                 f'{rows_behind:,} decision rows behind those markets are NOT the sample '
                 f'size &mdash; one market re-priced every minute is one outcome, not '
                 f'hundreds.</div>')

    # Hero tiles
    o.append('<div class="tiles">')
    o.append(f'<div class="tile"><div class="label">Settled markets</div>'
             f'<div class="value">{markets}</div>'
             f'<div class="foot">the real sample size</div></div>')
    if entries:
        o.append(f'<div class="tile"><div class="label">Trades taken</div>'
                 f'<div class="value">{len(entries)}</div>'
                 f'<div class="foot">met the edge bar</div></div>')
        o.append(f'<div class="tile"><div class="label">P&amp;L, 1 contract each</div>'
                 f'<div class="value">{_signed(pnl_1c)}</div>'
                 f'<div class="foot">win rate {100*wins/len(entries):.0f}%</div></div>')
    else:
        o.append('<div class="tile"><div class="label">Trades taken</div>'
                 '<div class="value">0</div>'
                 '<div class="foot">nothing met the edge bar</div></div>')
        o.append('<div class="tile"><div class="label">P&amp;L</div>'
                 '<div class="value">&mdash;</div>'
                 '<div class="foot">no trades, so no result</div></div>')
    o.append('</div>')

    o.append('<div class="card"><h2>Is the model honest? (calibration)</h2>'
             '<p class="note">Each dot is a group of predictions. On the dashed line, '
             '"70% likely" happens 70% of the time. Above it the model is too '
             'pessimistic, below it too confident.</p>'
             + _calibration_svg(calib) + '</div>')

    o.append('<div class="card"><h2>Does it sharpen as settlement approaches?</h2>'
             '<p class="note">Brier score by how long before the market closed the call '
             'was made. Lower is sharper. A big drop in the last bar is the live '
             'observation feed adding real information.</p>'
             + _brier_svg(brier) + '</div>')

    also_live = conn.execute(
        "SELECT COUNT(DISTINCT d.ticker) c FROM decisions d "
        "JOIN settlements s ON s.ticker = d.ticker WHERE d.gate_passed = 1").fetchone()["c"]
    account_note = ""
    if entries:
        account_note = (
            f' Of these {len(entries)}, <strong>{also_live}</strong> would also have '
            f'cleared the account-state gates (funded balance, exposure cap).'
            + (' That is zero because the account holds $0 &mdash; those gates govern '
               'whether you <em>may</em> trade, not whether the call was good.'
               if also_live == 0 else ''))
    o.append('<div class="card"><h2>Cumulative P&amp;L of trades the strategy would have taken</h2>'
             '<p class="note">One entry per market that met the edge bar, held to '
             'settlement, 1 contract each. This is the only P&amp;L on this page that '
             'describes a strategy anyone would actually run.' + account_note + '</p>'
             + _pnl_svg(entries) + '</div>')

    if blocks:
        total = sum(r["c"] for r in blocks)
        o.append('<div class="card"><h2>Why candidates were rejected</h2>'
                 '<p class="note">Gates firing constantly is them working. '
                 '<code>min_edge</code> dominating means the model rarely disagrees with '
                 'the market by enough to be worth the fee.</p><div class="scroll"><table>'
                 '<tr><th>gate</th><th class="num">times</th><th class="num">share</th></tr>')
        for r in blocks:
            o.append(f'<tr><td>{_esc(r["blocked_by"])}</td>'
                     f'<td class="num">{r["c"]:,}</td>'
                     f'<td class="num">{100*r["c"]/total:.0f}%</td></tr>')
        o.append('</table></div></div>')

    if entries:
        o.append('<div class="card"><h2>Every trade taken</h2>'
                 '<p class="note">The same numbers as the chart above, readable without '
                 'colour.</p><div class="scroll"><table>'
                 '<tr><th>when (UTC)</th><th>market</th><th>side</th>'
                 '<th class="num">paid</th><th class="num">spread</th>'
                 '<th>settled</th><th class="num">P&amp;L</th></tr>')
        for e in reversed(entries):
            fee = taker_fee_cents(e.price, 1)
            won = _won(e.side, e.result)
            pnl = ((100 - e.price) if won else -e.price) - fee
            when = datetime.fromtimestamp(e.ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
            sp = f'{e.spread}c' if e.spread is not None else "&mdash;"
            o.append(f'<tr><td>{when}</td><td>{_esc(e.ticker)}</td>'
                     f'<td>{_esc(e.side.upper())}</td>'
                     f'<td class="num">{e.price}c</td><td class="num">{sp}</td>'
                     f'<td>{"won" if won else "lost"}</td>'
                     f'<td class="num">{_signed(pnl)}</td></tr>')
        o.append('</table></div></div>')

    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    o.append('<footer><ul>'
             '<li>Entries assume the displayed price was actually available. Only real '
             'fills settle that; the report\'s paper-vs-live section is what measures it.</li>'
             '<li>Calibration counts decision rows, so a market that stayed tradable for '
             'hours is weighted more heavily than one that did not.</li>'
             f'<li>Generated {generated} from {_esc(db_path)}. Regenerate any time with '
             f'<code>./dashboard.sh</code>.</li>'
             '</ul></footer>')
    o.append('</div><div id="tip"></div>')

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_esc(family)} bot &middot; projectrebound</title>"
        f"<style>{_CSS}</style></head><body>"
        + "".join(o)
        + f"<script>{_JS}</script></body></html>"
    )
