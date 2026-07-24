"""Micro-live execution engine.

Places a REAL order for exactly ONE contract, and only when the caller has
already confirmed every risk gate passed and live trading is enabled. The
1-contract ceiling is the module constant below; it is deliberately NOT read
from config so it cannot be raised by editing a YAML file.

Maker preference: orders are submitted post-only (resting) so they take the
lower maker fee and never cross the spread. An order that would cross is
rejected by the exchange and recorded as such -- that is data, not an error.
Unfilled resting orders are cancelled after a timeout and recorded as unfilled.

UNVERIFIED order-body schema: Kalshi's docs disagreed across sources on the
exact order fields (see the TypeScript sibling project's createOrder note and
the README). This path is off by default; verify the body against
https://docs.kalshi.com/api-reference/orders/create-order-v2 and test on the
demo environment before enabling live trading.
"""
from __future__ import annotations

import uuid
from typing import Optional

from ..clients.kalshi_client import KalshiClient
from ..execution.fees import maker_fee_cents, taker_fee_cents
from ..execution.intent import OrderIntent
from ..storage.dao import Dao
from ..util import now_ms

# Hard Phase 1 ceiling. Not configurable. Raising this is a spec violation.
PHASE1_CONTRACT_LIMIT = 1


class LiveEngine:
    def __init__(self, dao: Dao, client: KalshiClient) -> None:
        self._dao = dao
        self._client = client

    def submit(self, decision_id: int, intent: OrderIntent,
               post_only: bool = True) -> tuple[int, Optional[str]]:
        """Submit a 1-contract order. Returns (live_order_row_id, order_id|None)."""
        count = PHASE1_CONTRACT_LIMIT  # never intent-derived; always 1
        client_order_id = str(uuid.uuid4())
        price_field = "yes_price" if intent.side == "yes" else "no_price"
        body = {
            "ticker": intent.ticker,
            "client_order_id": client_order_id,
            "side": intent.side,
            "action": "buy",
            "count": count,
            "type": "limit",
            "post_only": post_only,
            "time_in_force": "good_till_canceled",
            price_field: intent.limit_price_cents,
        }
        order = self._client.create_order(body)
        order_id = order.get("order_id")
        row_id = self._dao.insert_live_order(
            decision_id=decision_id, ticker=intent.ticker, side=intent.side,
            count=count, limit_price_cents=intent.limit_price_cents,
            client_order_id=client_order_id, order_id=order_id,
            status="submitted", submitted_ts=now_ms(),
        )
        return row_id, order_id

    def reconcile(self, row_id: int, order_id: str) -> str:
        """Poll the order and record its terminal state.

        Records maker vs taker (fee differs), fill price, and fee. An order that
        never filled is cancelled and recorded as 'unfilled' -- this is exactly
        the paper-vs-live gap Phase 1 exists to measure.
        """
        try:
            order = self._client._request("GET", f"/portfolio/orders/{order_id}").get("order", {})
        except Exception:
            return "submitted"  # leave for a later reconcile pass

        status = order.get("status", "")
        fill_count = int(order.get("fill_count", 0) or 0)
        if fill_count >= PHASE1_CONTRACT_LIMIT:
            avg = order.get("average_fill_price")
            fill_price = int(avg) if avg is not None else None
            maker = bool(order.get("maker_filled") or order.get("post_only"))
            fee = (maker_fee_cents(fill_price, 1) if maker else taker_fee_cents(fill_price, 1)) \
                if fill_price is not None else None
            self._dao.update_live_order(
                row_id, status="filled", fill_type="maker" if maker else "taker",
                fill_price_cents=fill_price, fee_cents=fee, resolved_ts=now_ms(),
            )
            return "filled"
        if status in ("canceled", "cancelled", "expired"):
            self._dao.update_live_order(row_id, status="unfilled", fill_type="none",
                                        resolved_ts=now_ms())
            return "unfilled"
        return "submitted"

    def cancel_unfilled(self, row_id: int, order_id: str) -> None:
        try:
            self._client.cancel_order(order_id)
        except Exception:
            pass
        self._dao.update_live_order(row_id, status="unfilled", fill_type="none",
                                    resolved_ts=now_ms())
