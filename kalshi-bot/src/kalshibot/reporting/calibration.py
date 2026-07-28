"""Phase 1 reporting -- the whole point of the exercise.

Everything here is computed from the raw append-only tables so numbers can be
re-derived and audited. Text output only (no dashboard, per spec).

The headline output is the paper-vs-live divergence: paper trading assumes
fills that would not happen on thin books, so the gap between paper P&L and
1-contract live P&L is the empirical slippage + adverse-selection cost.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..execution.fees import taker_fee_cents
from ..storage.dao import Dao

MIN_SAMPLE = 100  # below this, refuse to present numbers as conclusive


def _won(side: str, result: str) -> bool:
    return (side == "yes" and result == "yes") or (side == "no" and result == "no")


def _pnl_cents(side: str, price_cents: int, fee_cents: int, count: int, result: str) -> int:
    gross = (100 - price_cents) if _won(side, result) else (-price_cents)
    return gross * count - fee_cents


def _contracts_for_stake(price_cents: int, stake_cents: int) -> int:
    """How many contracts a given dollar stake buys at this price (>=1)."""
    if price_cents <= 0:
        return 0
    return max(1, stake_cents // price_cents)


def _scaled_pnl_cents(side: str, price_cents: int, result: str, stake_cents: int) -> tuple[int, int]:
    """(contracts, net P&L in cents) if `stake_cents` were deployed at entry.
    Fee recomputed (taker) for the scaled contract count. Hypothetical -- assumes
    the whole size fills at the displayed price, which thin books won't honor."""
    n = _contracts_for_stake(price_cents, stake_cents)
    if n == 0:
        return 0, 0
    gross = (100 - price_cents) if _won(side, result) else (-price_cents)
    fee = taker_fee_cents(price_cents, n)
    return n, gross * n - fee


def _fmt_usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents)/100:,.2f}"


def _hours(ms: int) -> float:
    return ms / 3_600_000.0


@dataclass
class Report:
    text: str
    resolved_count: int      # decision rows whose market has settled
    market_count: int = 0    # DISTINCT settled markets behind those rows


def generate_report(dao: Dao, stake_dollars: float = 10.0, ledger_rows: int = 25) -> Report:
    conn = dao.conn
    lines: list[str] = []
    stake_cents = int(round(stake_dollars * 100))

    resolved = conn.execute(
        "SELECT COUNT(*) AS c FROM decisions d JOIN settlements s ON s.ticker=d.ticker"
    ).fetchone()["c"]
    # The number that actually governs how much can be concluded. A decision row
    # is written every polling cycle, so ONE market that stayed tradable for six
    # hours contributes ~360 rows that are all the same bet on the same outcome.
    # Counting those as 360 samples would clear the minimum-sample bar on a
    # single day of collection and make a coin flip look like evidence.
    markets = conn.execute(
        "SELECT COUNT(DISTINCT d.ticker) AS c FROM decisions d "
        "JOIN settlements s ON s.ticker=d.ticker"
    ).fetchone()["c"]

    lines.append("=" * 68)
    lines.append("projectrebound Phase 1 -- validation report")
    lines.append("=" * 68)
    lines.append(f"Settled markets (the real sample size): {markets}")
    lines.append(f"Decision rows behind them:             {resolved}"
                 + (f"   (~{resolved // markets} polls per market)" if markets else ""))
    if markets < MIN_SAMPLE:
        lines.append("")
        lines.append(f"** SAMPLE TOO SMALL ({markets} settled markets < {MIN_SAMPLE}). **")
        lines.append("** Numbers below are shown for wiring/sanity only and must NOT be")
        lines.append("** read as evidence of edge or its absence. Keep collecting.")
        lines.append("** Note the row count above is NOT the sample size: the same market")
        lines.append("** re-priced every minute is one outcome, not hundreds.")
    lines.append("")

    _activity_section(conn, lines)
    _strategy_section(conn, lines, stake_cents, stake_dollars)
    _calibration_section(conn, lines)
    _accuracy_by_time_to_settlement(conn, lines)
    _edge_section(conn, lines)
    _pnl_section(conn, lines)
    _trade_ledger_section(conn, lines, stake_cents, stake_dollars, ledger_rows)
    _divergence_section(conn, lines)

    return Report(text="\n".join(lines), resolved_count=resolved, market_count=markets)


def _activity_section(conn, lines: list[str]) -> None:
    """Is it actually working? Distinguishes "healthy but young" (collecting,
    deciding, nothing settled yet) from "silently doing nothing" -- which
    otherwise look identical when the resolved-sample count is 0."""
    def one(sql: str, *args):
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else 0

    decisions = one("SELECT COUNT(*) FROM decisions")
    passed = one("SELECT COUNT(*) FROM decisions WHERE gate_passed=1")
    paper = one("SELECT COUNT(*) FROM paper_fills")
    pending = one("SELECT COUNT(*) FROM paper_fills f "
                  "WHERE f.ticker NOT IN (SELECT ticker FROM settlements)")
    books = one("SELECT COUNT(*) FROM book_snapshots")
    books_usable = one(
        "SELECT COUNT(*) FROM book_snapshots WHERE yes_levels != '[]' OR no_levels != '[]'")
    forecasts = one("SELECT COUNT(*) FROM forecasts")
    providers = one("SELECT COUNT(DISTINCT provider) FROM forecasts")
    obs = one("SELECT COUNT(*) FROM observations")
    ratings = one("SELECT COUNT(*) FROM sports_ratings")
    rating_sources = one("SELECT COUNT(DISTINCT provider) FROM sports_ratings")
    game_states = one("SELECT COUNT(*) FROM sports_game_states")
    games = one("SELECT COUNT(*) FROM sports_games")
    markets = one("SELECT COUNT(*) FROM markets")
    settled = one("SELECT COUNT(*) FROM settlements")
    last_book = one("SELECT MAX(captured_ts) FROM book_snapshots")
    model_inputs = forecasts + ratings

    lines.append("-- Activity (is it working?) --")
    lines.append(f"  data collected : {books} book snapshots ({books_usable} with live quotes)")
    # Only the running family's input lines are shown, so a weather report is
    # not padded with four sports zeros (and vice versa).
    if forecasts or not ratings:
        lines.append(f"  weather inputs : {forecasts} forecasts from {providers} sources, "
                     f"{obs} station observations")
    if ratings or games:
        lines.append(f"  sports inputs  : {ratings} ratings from {rating_sources} methods, "
                     f"{game_states} game states, {games} games linked")
    lines.append(f"  markets tracked: {markets}   settled so far: {settled}")
    lines.append(f"  decisions made : {decisions}  (passed all risk gates: {passed})")
    lines.append(f"  paper trades   : {paper}  ({pending} awaiting settlement)")

    blocked = conn.execute(
        "SELECT blocked_by, COUNT(*) AS c FROM decisions "
        "WHERE gate_passed=0 AND blocked_by IS NOT NULL GROUP BY blocked_by ORDER BY c DESC"
    ).fetchall()
    if blocked:
        detail = ", ".join(f"{r['blocked_by']} x{r['c']}" for r in blocked)
        lines.append(f"  blocked by     : {detail}")

    if last_book:
        age_min = (int(datetime.now(tz=timezone.utc).timestamp() * 1000) - last_book) / 60000
        lines.append(f"  last data point: {age_min:.0f} minutes ago")

    # Plain-language diagnosis, so a zero never has to be interpreted by hand.
    if decisions == 0:
        if books == 0:
            lines.append("  >> NOT COLLECTING. No order books stored -- check the run window for errors.")
        elif books_usable == 0 and books >= 200:
            # A genuinely thin market still quotes SOMETIMES. Hundreds of polls
            # with zero quotes is a parsing/shape bug, not market conditions --
            # this exact signature hid the orderbook_fp mismatch.
            lines.append(f"  >> SUSPICIOUS: {books} polls, not one with a quote. A thin market")
            lines.append("     would still quote occasionally. Likely an order-book parsing")
            lines.append("     problem rather than market conditions -- worth investigating.")
        elif books_usable == 0:
            lines.append("  >> Collecting, but no order book has carried a quote yet.")
            lines.append("     Can be normal for thin markets; re-check once trading is active.")
        elif model_inputs == 0:
            lines.append("  >> Collecting prices but NO model inputs (forecasts / ratings) --")
            lines.append("     the data sources are failing. Check the log for provider warnings.")
        elif ratings and games == 0:
            lines.append("  >> Ratings exist but no game is linked to a market. Usually a team-code")
            lines.append("     mismatch -- see the 'no ESPN game matched' warnings in the log.")
        else:
            lines.append("  >> Data is arriving but no decisions yet. Usually means no market has")
            lines.append("     cleared the minimum-edge bar -- which is the gates doing their job.")
    elif settled == 0:
        lines.append("  >> WORKING. Decisions are being made; nothing has settled yet, so the")
        lines.append("     scoring sections below stay empty until markets resolve (daily).")
    lines.append("")


def _accuracy_by_time_to_settlement(conn, lines: list[str]) -> None:
    """Does the model get sharper as settlement approaches? Buckets each
    decision by how long before the market closed it was made, and scores each
    bucket. If the near-settlement bucket is much sharper, that's the live
    observation edge showing up -- and a reason to trade later in the day."""
    rows = conn.execute(
        """
        SELECT d.probability AS p, d.decision_ts AS dts, m.close_ts AS close_ts, s.result AS result
        FROM decisions d
        JOIN settlements s ON s.ticker = d.ticker
        JOIN markets m ON m.ticker = d.ticker
        WHERE m.close_ts IS NOT NULL
        """
    ).fetchall()
    lines.append("-- Model accuracy by time before settlement --")
    if not rows:
        lines.append("  (need resolved markets with a known close time)")
        lines.append("")
        return
    # (label, lower_hours_inclusive, upper_hours_exclusive)
    buckets = [(">24h before", 24, 1e9), ("6-24h before", 6, 24),
               ("2-6h before", 2, 6), ("<2h before", 0, 2)]
    lines.append("  window          n    Brier   (lower = sharper)")
    for label, lo, hi in buckets:
        items = [r for r in rows
                 if lo <= _hours(r["close_ts"] - r["dts"]) < hi]
        if not items:
            continue
        brier = sum((float(r["p"]) - (1 if r["result"] == "yes" else 0)) ** 2 for r in items) / len(items)
        lines.append(f"  {label:<14} {len(items):>3}   {brier:.4f}")
    lines.append("")


def _trade_ledger_section(conn, lines: list[str], stake_cents: int,
                          stake_dollars: float, ledger_rows: int) -> None:
    """Every paper trade: what it bought, what it settled at, and the P&L --
    both at 1 contract and scaled to a chosen dollar stake per trade."""
    all_rows = conn.execute(
        """
        SELECT f.filled_ts, f.ticker, f.side, f.price_cents, f.fee_cents, s.result
        FROM paper_fills f JOIN settlements s ON s.ticker = f.ticker
        ORDER BY f.filled_ts DESC
        """
    ).fetchall()

    lines.append("-- Every candidate considered (NOT a strategy -- see above) --")
    lines.append("   One row per market per polling cycle, gates ignored, negative-edge")
    lines.append("   candidates included. Buying the same market 900 times pays the fee")
    lines.append("   900 times on one outcome, so these totals are guaranteed to look")
    lines.append("   terrible and mean nothing. Use the strategy section above.")
    if not all_rows:
        lines.append("  (no settled paper records yet)")
        lines.append("")
        return

    total_1c = 0
    total_stake = 0
    wins = 0
    for r in all_rows:
        total_1c += _pnl_cents(r["side"], r["price_cents"], r["fee_cents"], 1, r["result"])
        _, sp = _scaled_pnl_cents(r["side"], r["price_cents"], r["result"], stake_cents)
        total_stake += sp
        if _won(r["side"], r["result"]):
            wins += 1

    n = len(all_rows)
    lines.append(f"  date        ticker                            side  buy   settled   P&L(1)   P&L(${stake_dollars:.0f})")
    for r in all_rows[:ledger_rows]:
        won = _won(r["side"], r["result"])
        settled = "WON $1.00" if won else "LOST $.00"
        pnl1 = _pnl_cents(r["side"], r["price_cents"], r["fee_cents"], 1, r["result"])
        contracts, pnls = _scaled_pnl_cents(r["side"], r["price_cents"], r["result"], stake_cents)
        when = datetime.fromtimestamp(r["filled_ts"] / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        tick = (r["ticker"] or "")[:31]
        lines.append(f"  {when}  {tick:<31}  {r['side']:<4}  {r['price_cents']:>2}c  "
                     f"{settled:<9}  {_fmt_usd(pnl1):>6}  {_fmt_usd(pnls):>8}")
    if n > ledger_rows:
        lines.append(f"  ... and {n - ledger_rows} older trades (full history in the database)")
    lines.append("")
    lines.append(f"  Totals over {n} candidate records:  win rate {100*wins/n:.1f}%")
    lines.append(f"    at 1 contract each : {_fmt_usd(total_1c)}")
    lines.append(f"    at ${stake_dollars:.0f} each        : {_fmt_usd(total_stake)}")
    lines.append("    ^ NOT a strategy result. See the strategy section above.")
    lines.append("")


def _strategy_section(conn, lines: list[str], stake_cents: int,
                      stake_dollars: float) -> None:
    """What a strategy someone would actually run produced: ONE entry per
    market, taken only when every risk gate passed, held to settlement.

    This exists because the paper ledger below is not a strategy and cannot be
    read as one. `decide()` has no minimum-edge filter -- it returns the better
    of the two sides even when the expected value is negative -- and the paper
    record is written for every candidate on every polling cycle regardless of
    the gates, because it is research data about the model, not a trade log. So
    the paper totals describe buying every market every minute with the risk
    limits switched off, which nobody would do and which loses money by
    construction: the fee is paid hundreds of times on one outcome.

    The number below is the honest one. It is still optimistic (it assumes the
    displayed price was there for the taking), and the paper-vs-live divergence
    section is what eventually measures that.
    """
    rows = conn.execute(
        """
        SELECT d.ticker, d.intended_side AS side, d.intended_price_cents AS price,
               MIN(d.decision_ts) AS entry_ts, d.edge_after_fees AS edge,
               d.spread_cents AS spread, s.result AS result
        FROM decisions d
        JOIN settlements s ON s.ticker = d.ticker
        WHERE d.gate_passed = 1 AND d.intended_side IS NOT NULL
          AND d.intended_price_cents IS NOT NULL
        GROUP BY d.ticker
        """
    ).fetchall()
    settled_markets = conn.execute(
        "SELECT COUNT(DISTINCT d.ticker) AS c FROM decisions d "
        "JOIN settlements s ON s.ticker = d.ticker"
    ).fetchone()["c"]

    lines.append("-- Strategy: one entry per market, gates enforced (THE number) --")
    if not rows:
        lines.append(f"  No market cleared every risk gate, out of {settled_markets} settled.")
        lines.append("  That is the gates working, not a failure: with no edge above the")
        lines.append("  minimum, the correct number of trades is zero.")
        lines.append("")
        return

    total_1c = total_scaled = wins = 0
    for r in rows:
        fee = taker_fee_cents(r["price"], 1)
        total_1c += _pnl_cents(r["side"], r["price"], fee, 1, r["result"])
        _, scaled = _scaled_pnl_cents(r["side"], r["price"], r["result"], stake_cents)
        total_scaled += scaled
        if _won(r["side"], r["result"]):
            wins += 1

    n = len(rows)
    avg_price = sum(r["price"] for r in rows) / n
    avg_edge = sum(float(r["edge"] or 0) for r in rows) / n
    lines.append(f"  markets entered : {n} of {settled_markets} settled "
                 f"({settled_markets - n} never cleared the gates)")
    lines.append(f"  win rate        : {100*wins/n:.1f}%  ({wins}/{n})")
    lines.append(f"  average entry   : {avg_price:.0f}c, claimed edge {avg_edge:+.3f}/contract")
    spreads = [r["spread"] for r in rows if r["spread"] is not None]
    if spreads:
        avg_spread = sum(spreads) / len(spreads)
        lines.append(f"  average spread  : {avg_spread:.1f}c at entry"
                     + ("   <-- wider than the edge claimed above"
                        if avg_spread / 100 > avg_edge else ""))
    lines.append(f"  {'P&L at 1 each':<16}: {_fmt_usd(total_1c)}")
    lines.append(f"  {f'P&L at ${stake_dollars:.0f} each':<16}: {_fmt_usd(total_scaled)}")
    if n < MIN_SAMPLE:
        lines.append(f"  ({n} markets is far too few to conclude anything either way.)")
    lines.append("")


def _calibration_section(conn, lines: list[str]) -> None:
    rows = conn.execute(
        """
        SELECT d.probability AS p, s.result AS result
        FROM decisions d JOIN settlements s ON s.ticker = d.ticker
        """
    ).fetchall()
    lines.append("-- Calibration (model P(YES) vs observed YES frequency) --")
    lines.append("   (n counts decision rows, not games: a market priced every minute")
    lines.append("    for hours appears many times, so treat n as weight, not sample size)")
    if not rows:
        lines.append("  (no resolved decisions yet)")
        lines.append("")
        return

    buckets: dict[int, list[tuple[float, int]]] = {i: [] for i in range(10)}
    brier = 0.0
    logloss = 0.0
    eps = 1e-9
    for r in rows:
        p = float(r["p"])
        y = 1 if r["result"] == "yes" else 0
        b = min(9, int(p * 10))
        buckets[b].append((p, y))
        brier += (p - y) ** 2
        pc = min(1 - eps, max(eps, p))
        logloss += -(y * math.log(pc) + (1 - y) * math.log(1 - pc))

    n = len(rows)
    lines.append("  decile   n   pred%   obs%")
    for b in range(10):
        items = buckets[b]
        if not items:
            continue
        pred = 100 * sum(p for p, _ in items) / len(items)
        obs = 100 * sum(y for _, y in items) / len(items)
        lines.append(f"  {b*10:>3}-{b*10+10:<3} {len(items):>3}  {pred:5.1f}  {obs:5.1f}")
    lines.append(f"  Brier score: {brier / n:.4f}   (lower is better; 0.25 = always 50%)")
    lines.append(f"  Log loss:    {logloss / n:.4f}")
    lines.append("")


def _edge_section(conn, lines: list[str]) -> None:
    rows = conn.execute(
        "SELECT edge_after_fees FROM decisions WHERE edge_after_fees IS NOT NULL"
    ).fetchall()
    lines.append("-- Edge distribution (model EV per contract, $, at decision time) --")
    if not rows:
        lines.append("  (no decisions with computed edge yet)")
        lines.append("")
        return
    edges = sorted(float(r["edge_after_fees"]) for r in rows)
    lines.append(f"  n={len(edges)}  min={edges[0]:+.3f}  "
                 f"median={edges[len(edges)//2]:+.3f}  max={edges[-1]:+.3f}")
    # Edge is computed at the ask, so it is already net of the spread -- but
    # seeing the two side by side is what tells you whether the "edge" was a
    # disagreement with the market or just a wide quote.
    sp = conn.execute(
        "SELECT spread_cents FROM decisions WHERE spread_cents IS NOT NULL"
    ).fetchall()
    if sp:
        vals = sorted(r["spread_cents"] for r in sp)
        wide = sum(1 for v in vals if v > 5)
        lines.append(f"  spread at those decisions: median {vals[len(vals)//2]}c, "
                     f"{100*wide/len(vals):.0f}% wider than 5c")
    lines.append("")


def _pnl_section(conn, lines: list[str]) -> None:
    paper = conn.execute(
        """
        SELECT f.side, f.price_cents, f.fee_cents, f.count, s.result
        FROM paper_fills f JOIN settlements s ON s.ticker = f.ticker
        """
    ).fetchall()
    lines.append("-- P&L (net of modelled fees) --")
    if paper:
        total = sum(_pnl_cents(r["side"], r["price_cents"], r["fee_cents"], r["count"], r["result"])
                    for r in paper)
        fees = sum(r["fee_cents"] for r in paper)
        gross = total + fees
        lines.append(f"  Paper: {len(paper)} settled candidate records (not trades), "
                     f"net {total/100:+.2f} USD, fees {fees/100:.2f} USD")
        if gross != 0:
            lines.append(f"  Fee drag (paper): {100*fees/abs(gross):.1f}% of gross P&L")
    else:
        lines.append("  Paper: (no settled paper fills yet)")

    live = conn.execute(
        """
        SELECT o.side, o.fill_price_cents, o.fee_cents, s.result
        FROM live_orders o JOIN settlements s ON s.ticker = o.ticker
        WHERE o.status = 'filled' AND o.fill_price_cents IS NOT NULL
        """
    ).fetchall()
    if live:
        total = sum(_pnl_cents(r["side"], r["fill_price_cents"], r["fee_cents"] or 0, 1, r["result"])
                    for r in live)
        lines.append(f"  Live : {len(live)} settled 1-contract fills, net {total/100:+.2f} USD")
    else:
        lines.append("  Live : (no settled live fills yet)")
    lines.append("")


def _divergence_section(conn, lines: list[str]) -> None:
    lines.append("-- Paper vs live divergence (the headline number) --")
    # Match paper and live records that came from the same decision.
    rows = conn.execute(
        """
        SELECT p.price_cents AS paper_price, o.status AS live_status,
               o.fill_price_cents AS live_price, o.side AS side, s.result AS result,
               p.fee_cents AS paper_fee, o.fee_cents AS live_fee
        FROM paper_fills p
        JOIN live_orders o ON o.decision_id = p.decision_id
        JOIN settlements s ON s.ticker = p.ticker
        """
    ).fetchall()
    if not rows:
        lines.append("  (no decisions with both a paper record and a live order yet --")
        lines.append("   this section fills in once micro-live trading has run.)")
        lines.append("")
        return

    unfilled = sum(1 for r in rows if r["live_status"] != "filled")
    filled = [r for r in rows if r["live_status"] == "filled" and r["live_price"] is not None]
    slippage = [r["live_price"] - r["paper_price"] for r in filled]
    lines.append(f"  matched decisions: {len(rows)}")
    lines.append(f"  live unfilled/cancelled (paper assumed a fill here): {unfilled}")
    if slippage:
        mean_slip = sum(slippage) / len(slippage)
        lines.append(f"  mean fill-price slippage (live - paper): {mean_slip:+.2f} cents")
    if filled:
        live_winrate = 100 * sum(1 for r in filled if _won(r["side"], r["result"])) / len(filled)
        lines.append(f"  live filled win rate: {live_winrate:.1f}%  "
                     f"(low vs paper suggests adverse selection)")
    lines.append("")
