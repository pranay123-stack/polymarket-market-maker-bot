"""Conservative paper-fill simulator driven by REAL Polymarket trade prints.

Paper mode posts nothing, so without this a paper run never fills and never
produces PnL. This module decides when a resting paper order *would* have been
filled, using only the live `last_trade_price` prints from the market WS:

- A resting BUY at price `b` can only be hit by an aggressive SELL on the SAME
  token. A SELL at `a` can only be lifted by an aggressive BUY on the same token.
- Trade-THROUGH (print strictly better than our price for the aggressor, e.g. a
  sell print below our bid): the aggressor swept our level, so we fill
  `min(remaining, print size)`.
- Trade-AT our price: we were queued behind the size that rested at that level
  when we placed. Prints at our price first consume that queue; only the excess
  fills us. Cancellations ahead of us are ignored (that would move us up the
  queue), so the queue model can only under-fill, never over-fill.
- Prints timestamped before our order existed never fill it.

What it does NOT model (all of which make the live result differ): the YES/NO
mirrored-book "mint" matches (a YES buyer matching our NO bid prints on the YES
token), maker rebates, liquidity rewards, and exchange latency. The first two
mean real fills and real income would be HIGHER than simulated; adverse
selection is captured because fills only happen when real flow trades into us.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass

from polymaker.domain import OpenOrder, Side, TradeState
from polymaker.logging import get_logger
from polymaker.marketdata.parse import TradePrint
from polymaker.state.tracker import TradeEvent

log = get_logger("paper.fills")

_EPS = 1e-9


@dataclass(slots=True)
class _Queue:
    ahead: float  # size resting at our price when we joined the level
    placed_ts: float


@dataclass(frozen=True, slots=True)
class PaperFill:
    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    remaining: float  # order size left after this fill
    trade_id: str
    ts: float


class PaperFillSimulator:
    """Tracks queue position per paper order and matches it against prints."""

    def __init__(self, level_size: Callable[[str, Side, float], float]) -> None:
        # level_size(token, side, price) -> size currently resting at that level
        self._level_size = level_size
        self._queues: dict[str, _Queue] = {}
        self._ids = itertools.count(1)

    def on_placed(self, order: OpenOrder) -> None:
        if order.order_id in self._queues:
            return
        ahead = self._level_size(order.token_id, order.side, order.price)
        self._queues[order.order_id] = _Queue(ahead=max(0.0, ahead), placed_ts=order.created_ts)

    def forget(self, order_id: str) -> None:
        self._queues.pop(order_id, None)

    def match(self, tp: TradePrint, resting: list[OpenOrder]) -> list[PaperFill]:
        """Fills produced by one real trade print against our resting orders."""
        # the side of OUR order this aggressor can trade against
        maker_side = tp.aggressor.opposite
        # best-priced first: an aggressive sell hits the highest bid first
        candidates = sorted(
            (o for o in resting if o.token_id == tp.asset_id and o.side is maker_side and o.size > 0),
            key=lambda o: -o.price if maker_side is Side.BUY else o.price,
        )
        left = tp.size
        out: list[PaperFill] = []
        for o in candidates:
            if left <= _EPS:
                break
            q = self._queues.get(o.order_id)
            if q is None:
                continue  # never saw it placed -> no queue position, don't guess
            if tp.ts + _EPS < q.placed_ts:
                continue  # print predates the order
            if maker_side is Side.BUY:
                through = tp.price < o.price - _EPS
                at = abs(tp.price - o.price) <= _EPS
            else:
                through = tp.price > o.price + _EPS
                at = abs(tp.price - o.price) <= _EPS
            if not (through or at):
                continue
            avail = left
            if at:
                eaten = min(q.ahead, avail)
                q.ahead -= eaten
                avail -= eaten
                left -= eaten
            if avail <= _EPS:
                continue
            size = min(o.size, avail)
            left -= size
            remaining = max(0.0, o.size - size)
            out.append(PaperFill(
                order_id=o.order_id, token_id=o.token_id, side=o.side,
                price=o.price, size=size, remaining=remaining,
                trade_id=f"paper-fill-{next(self._ids)}-{o.order_id}", ts=tp.ts,
            ))
            if remaining <= _EPS:
                self._queues.pop(o.order_id, None)
        return out


def as_trade_events(f: PaperFill) -> tuple[TradeEvent, TradeEvent]:
    """MATCHED then CONFIRMED events, so a paper fill takes the exact same
    lifecycle path through UserEventProcessor as a live user-WS fill."""
    m = TradeEvent(f.token_id, f.side, f.price, f.size, f.trade_id, TradeState.MATCHED, f.ts)
    c = TradeEvent(f.token_id, f.side, f.price, f.size, f.trade_id, TradeState.CONFIRMED, f.ts)
    return m, c
