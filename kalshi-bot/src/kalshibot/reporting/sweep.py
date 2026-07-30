"""Parameter sweep over already-collected decisions.

The point: you do not have to wait for new data to test a different threshold.
Every decision ever made is stored with the probability, the price, the spread,
and (once the market resolved) the outcome -- so "what would a 3c minimum edge
and a 3c maximum spread have done?" is a query, not another week of collecting.

What this sweeps is the GATES, not the model. It re-decides which of the
already-recorded candidates a different threshold would have taken, and scores
them. Changing the model itself (a different sigma, a different clamp) needs a
full replay through the AsOfView and is a bigger job -- see README.

Two honest limits, both stated in the output rather than buried here:

1. **It is in-sample.** Picking the best cell of a table you fit on the same
   data is how backtests lie. The output therefore splits the history in half by
   date and shows both, so a setting that only worked in one half is visible as
   exactly that.
2. **It assumes the displayed price was available.** Same optimism as the paper
   ledger. Only real fills settle that question.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..execution.fees import taker_fee_cents
from ..storage.dao import Dao

# Defaults chosen to bracket the current configuration (5c edge, no spread cap)
# rather than to flatter it.
DEFAULT_EDGES = (0.02, 0.03, 0.05, 0.08, 0.12)
DEFAULT_SPREADS = (2, 3, 5, 99)


@dataclass(frozen=True)
class Candidate:
    ticker: str
    ts: int
    side: str
    price: int
    edge: float
    spread: Optional[int]
    result: str
    prob: float = 0.0


# The two kinds of gate, kept apart on purpose.
#
# DECISION-QUALITY gates answer "is this trade worth taking?" -- minimum edge,
# maximum spread, the price band. They are a property of the opportunity.
#
# ACCOUNT-STATE gates answer "may I trade at all right now?" -- funded balance,
# exposure caps, open-position count, the kill switch. They are a property of the
# account, and with an unfunded account they block EVERYTHING: on live data 5,453
# candidates cleared both min_edge and the price band, and `max_exposure` then
# rejected all 5,453 because the balance was $0.
#
# So the research question -- would these calls have made money? -- must be
# answered with the decision-quality gates only. Scoring on the account-state
# gates as well reports a permanent zero and says nothing about the model.
DECISION_QUALITY_DEFAULTS = {"min_edge": 0.05, "max_spread": None,
                             "price_band": (40, 60)}


def quality_thresholds(risk_cfg: Optional[dict]) -> dict:
    """Decision-quality thresholds from a risk config, falling back to defaults."""
    if not risk_cfg:
        return dict(DECISION_QUALITY_DEFAULTS)
    cap = risk_cfg.get("max_spread_cents")
    return {
        "min_edge": float(risk_cfg.get("min_edge_after_fees", 0.05)),
        "max_spread": None if cap is None else int(cap),
        "price_band": (int(float(risk_cfg.get("price_band_reject_low", 0.40)) * 100),
                       int(float(risk_cfg.get("price_band_reject_high", 0.60)) * 100)),
    }


@dataclass(frozen=True)
class Outcome:
    entered: int
    wins: int
    pnl_1c: int
    pnl_stake: int

    @property
    def win_rate(self) -> float:
        return 100.0 * self.wins / self.entered if self.entered else 0.0


def _won(side: str, result: str) -> bool:
    return (side == "yes" and result == "yes") or (side == "no" and result == "no")


def load_candidates(dao: Dao) -> list[Candidate]:
    """Every recorded candidate whose market has since settled.

    Deliberately ignores `gate_passed`: that column records the verdict of the
    thresholds in force at the time, and the whole point here is to ask what a
    different verdict would have produced.
    """
    rows = dao.conn.execute(
        """
        SELECT d.ticker, d.decision_ts AS ts, d.intended_side AS side,
               d.intended_price_cents AS price, d.edge_after_fees AS edge,
               d.spread_cents AS spread, d.probability AS prob, s.result AS result
        FROM decisions d
        JOIN settlements s ON s.ticker = d.ticker
        WHERE d.intended_side IS NOT NULL AND d.intended_price_cents IS NOT NULL
          AND d.edge_after_fees IS NOT NULL
        ORDER BY d.decision_ts
        """
    ).fetchall()
    return [Candidate(ticker=r["ticker"], ts=r["ts"], side=r["side"], price=r["price"],
                      edge=float(r["edge"]), spread=r["spread"], result=r["result"],
                      prob=float(r["prob"] or 0.0))
            for r in rows]


def first_qualifying_per_market(candidates: list[Candidate], *, min_edge: float,
                                max_spread: Optional[int],
                                price_band: tuple[int, int] = (40, 60)
                                ) -> list[Candidate]:
    """The FIRST candidate per market that clears the decision-quality gates.

    One entry per market, because the same market re-priced every minute is one
    opportunity, not hundreds -- counting each poll would multiply both the wins
    and the fees by the length of time the market happened to stay open. First,
    not best: picking the best price in hindsight is lookahead.

    The single definition of "a trade this strategy would take", shared by the
    sweep, the report and the dashboard so the three cannot drift apart.
    """
    entered: dict[str, Candidate] = {}
    for c in candidates:
        if c.ticker in entered:
            continue
        if c.edge < min_edge:
            continue
        if price_band[0] <= c.price <= price_band[1]:
            continue
        if max_spread is not None:
            # No recorded spread means the row predates spread recording; it
            # cannot satisfy a spread limit, so it is skipped rather than
            # assumed acceptable.
            if c.spread is None or c.spread > max_spread:
                continue
        entered[c.ticker] = c
    return list(entered.values())


def simulate(candidates: list[Candidate], *, min_edge: float, max_spread: Optional[int],
             stake_cents: int, price_band: tuple[int, int] = (40, 60)) -> Outcome:
    """Score the qualifying entries, held to settlement."""
    entered = {c.ticker: c for c in first_qualifying_per_market(
        candidates, min_edge=min_edge, max_spread=max_spread, price_band=price_band)}

    wins = pnl_1c = pnl_stake = 0
    for c in entered.values():
        won = _won(c.side, c.result)
        wins += 1 if won else 0
        pnl_1c += ((100 - c.price) if won else -c.price) - taker_fee_cents(c.price, 1)
        n = max(1, stake_cents // c.price) if c.price > 0 else 0
        if n:
            pnl_stake += ((100 - c.price) if won else -c.price) * n - taker_fee_cents(c.price, n)
    return Outcome(entered=len(entered), wins=wins, pnl_1c=pnl_1c, pnl_stake=pnl_stake)


def _fmt(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents)/100:,.2f}"


def _table(lines: list[str], candidates: list[Candidate], edges, spreads,
           stake_cents: int) -> None:
    header = "  min_edge |" + "".join(f"  spread<={s if s < 99 else 'any':>4}" for s in spreads)
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for e in edges:
        cells = []
        for sp in spreads:
            o = simulate(candidates, min_edge=e, max_spread=None if sp >= 99 else sp,
                         stake_cents=stake_cents)
            cells.append(f"  {o.entered:>3}/{_fmt(o.pnl_1c):>8}" if o.entered
                         else f"  {'--':>12}")
        lines.append(f"    {e:>6.2f} |" + "".join(cells))


def generate_sweep(dao: Dao, stake_dollars: float = 10.0, edges=DEFAULT_EDGES,
                   spreads=DEFAULT_SPREADS) -> str:
    stake_cents = int(round(stake_dollars * 100))
    candidates = load_candidates(dao)
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("Parameter sweep over collected decisions (gates only, not the model)")
    lines.append("=" * 70)

    if not candidates:
        lines.append("No settled decisions yet -- nothing to sweep.")
        return "\n".join(lines)

    markets = len({c.ticker for c in candidates})
    with_spread = sum(1 for c in candidates if c.spread is not None)
    lines.append(f"candidates: {len(candidates)} across {markets} settled markets")
    if with_spread == 0:
        lines.append("")
        lines.append("NOTE: no candidate has a recorded spread, so every spread-limited")
        lines.append("column below is empty. Spread recording started in schema v4 --")
        lines.append("those columns fill in as new decisions accumulate.")
    elif with_spread < len(candidates):
        lines.append(f"(of which {with_spread} have a recorded spread; older rows predate it)")
    lines.append("")
    lines.append("Each cell: markets entered / P&L at 1 contract each.")
    lines.append("")

    lines.append("-- All history --")
    _table(lines, candidates, edges, spreads, stake_cents)
    lines.append("")

    # Split by date. A setting that only works in one half is overfitting, and
    # this is the cheapest way to see it without pretending to do more.
    mid = candidates[len(candidates) // 2].ts
    first = [c for c in candidates if c.ts < mid]
    second = [c for c in candidates if c.ts >= mid]
    if first and second:
        lines.append("-- First half of the history --")
        _table(lines, first, edges, spreads, stake_cents)
        lines.append("")
        lines.append("-- Second half (settings that only work in one half are noise) --")
        _table(lines, second, edges, spreads, stake_cents)
        lines.append("")

    lines.append("Reading this honestly:")
    lines.append("  * The best cell is not a discovery. With this many cells, the best one")
    lines.append("    looks good by chance alone -- that is what the two halves are for.")
    lines.append("  * Entering few markets with a big number is weaker evidence than")
    lines.append("    entering many with a small one.")
    lines.append("  * Every entry assumes the displayed price was actually available.")
    return "\n".join(lines)
