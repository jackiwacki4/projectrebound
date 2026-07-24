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
from typing import Optional

from ..storage.dao import Dao

MIN_SAMPLE = 100  # below this, refuse to present numbers as conclusive


def _won(side: str, result: str) -> bool:
    return (side == "yes" and result == "yes") or (side == "no" and result == "no")


def _pnl_cents(side: str, price_cents: int, fee_cents: int, count: int, result: str) -> int:
    gross = (100 - price_cents) if _won(side, result) else (-price_cents)
    return gross * count - fee_cents


@dataclass
class Report:
    text: str
    resolved_count: int


def generate_report(dao: Dao) -> Report:
    conn = dao.conn
    lines: list[str] = []

    resolved = conn.execute(
        "SELECT COUNT(*) AS c FROM decisions d JOIN settlements s ON s.ticker=d.ticker"
    ).fetchone()["c"]

    lines.append("=" * 64)
    lines.append("projectrebound Phase 1 -- validation report")
    lines.append("=" * 64)
    lines.append(f"Resolved decisions (sample size): {resolved}")
    if resolved < MIN_SAMPLE:
        lines.append("")
        lines.append(f"** SAMPLE TOO SMALL ({resolved} < {MIN_SAMPLE}). **")
        lines.append("** Numbers below are shown for wiring/sanity only and must NOT be")
        lines.append("** read as evidence of edge or its absence. Keep collecting.")
    lines.append("")

    _calibration_section(conn, lines)
    _edge_section(conn, lines)
    _pnl_section(conn, lines)
    _divergence_section(conn, lines)

    return Report(text="\n".join(lines), resolved_count=resolved)


def _calibration_section(conn, lines: list[str]) -> None:
    rows = conn.execute(
        """
        SELECT d.probability AS p, s.result AS result
        FROM decisions d JOIN settlements s ON s.ticker = d.ticker
        """
    ).fetchall()
    lines.append("-- Calibration (model P(YES) vs observed YES frequency) --")
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
        lines.append(f"  Paper: {len(paper)} settled fills, net {total/100:+.2f} USD, "
                     f"fees {fees/100:.2f} USD")
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
