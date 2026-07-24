"""Paper execution engine.

Records the hypothetical fill at the best displayed price for the FULL intended
size, net of the modelled taker fee. This intentionally overestimates fills on
thin books -- the gap versus the 1-contract live path is the measurement we're
after. Marks to settlement at report time from the settlements table.
"""
from __future__ import annotations

from ..storage.dao import Dao
from ..util import now_ms
from .fees import taker_fee_cents
from .intent import OrderIntent


class PaperEngine:
    def __init__(self, dao: Dao) -> None:
        self._dao = dao

    def record(self, decision_id: int, intent: OrderIntent) -> int:
        count = intent.intended_paper_count
        fee = taker_fee_cents(intent.limit_price_cents, count)
        return self._dao.insert_paper_fill(
            decision_id=decision_id,
            ticker=intent.ticker,
            side=intent.side,
            count=count,
            price_cents=intent.limit_price_cents,
            fee_cents=fee,
            filled_ts=now_ms(),
        )
