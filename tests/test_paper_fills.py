"""Paper fill simulator: queue position, trade-through, side and time gating.

Pure-logic tests: the inputs are hand-built prints and orders by necessity
(the simulator's contract is about edge cases that have to be pinned exactly).
"""

from polymaker.domain import OpenOrder, Side, TradeState
from polymaker.marketdata.parse import TradePrint
from polymaker.paper.fills import PaperFillSimulator, as_trade_events
from polymaker.state.store import StateStore
from polymaker.state.tracker import UserEventProcessor

TOK = "tok-yes"


def _print(side: Side, price: float, size: float, ts: float = 100.0, tok: str = TOK) -> TradePrint:
    return TradePrint(asset_id=tok, condition_id="c", aggressor=side, price=price, size=size, ts=ts)


def _order(oid: str, side: Side, price: float, size: float, ts: float = 50.0) -> OpenOrder:
    return OpenOrder(oid, TOK, side, price, size, created_ts=ts)


def _sim(level: float) -> PaperFillSimulator:
    return PaperFillSimulator(lambda _t, _s, _p: level)


def test_trade_through_fills_up_to_print_size():
    sim = _sim(level=1_000.0)  # big queue ahead is irrelevant on a trade-through
    o = _order("a", Side.BUY, 0.50, 100)
    sim.on_placed(o)
    fills = sim.match(_print(Side.SELL, 0.49, 30), [o])
    assert len(fills) == 1
    assert fills[0].size == 30 and fills[0].price == 0.50 and fills[0].remaining == 70


def test_at_price_print_must_eat_the_queue_first():
    sim = _sim(level=50.0)
    o = _order("a", Side.BUY, 0.50, 100)
    sim.on_placed(o)
    assert sim.match(_print(Side.SELL, 0.50, 40), [o]) == []  # queue 50 -> 10
    fills = sim.match(_print(Side.SELL, 0.50, 25), [o])        # 10 queue, 15 to us
    assert [f.size for f in fills] == [15]


def test_wrong_side_or_worse_price_never_fills():
    sim = _sim(level=0.0)
    o = _order("a", Side.BUY, 0.50, 100)
    sim.on_placed(o)
    assert sim.match(_print(Side.BUY, 0.40, 50), [o]) == []   # buyer can't hit our bid
    assert sim.match(_print(Side.SELL, 0.51, 50), [o]) == []  # sold above our bid
    assert sim.match(_print(Side.SELL, 0.40, 50, tok="other"), [o]) == []


def test_sell_order_is_lifted_by_aggressive_buy():
    sim = _sim(level=0.0)
    o = _order("s", Side.SELL, 0.60, 20)
    sim.on_placed(o)
    fills = sim.match(_print(Side.BUY, 0.61, 50), [o])
    assert [(f.side, f.size, f.remaining) for f in fills] == [(Side.SELL, 20, 0)]


def test_print_before_order_existed_is_ignored():
    sim = _sim(level=0.0)
    o = _order("a", Side.BUY, 0.50, 100, ts=200.0)
    sim.on_placed(o)
    assert sim.match(_print(Side.SELL, 0.40, 50, ts=150.0), [o]) == []


def test_unknown_order_has_no_queue_and_does_not_fill():
    sim = _sim(level=0.0)
    o = _order("a", Side.BUY, 0.50, 100)
    assert sim.match(_print(Side.SELL, 0.40, 50), [o]) == []


def test_print_size_is_shared_best_price_first():
    sim = _sim(level=0.0)
    hi = _order("hi", Side.BUY, 0.50, 10)
    lo = _order("lo", Side.BUY, 0.48, 10)
    for o in (hi, lo):
        sim.on_placed(o)
    fills = sim.match(_print(Side.SELL, 0.47, 15), [lo, hi])
    assert [(f.order_id, f.size) for f in fills] == [("hi", 10), ("lo", 5)]


def test_paper_fill_takes_the_live_lifecycle_path(tmp_path):
    store = StateStore(tmp_path / "s.db")
    seen = []
    proc = UserEventProcessor(store, on_fill=seen.append)
    sim = _sim(level=0.0)
    o = _order("a", Side.BUY, 0.50, 100)
    sim.on_placed(o)
    (f,) = sim.match(_print(Side.SELL, 0.45, 40), [o])
    m, c = as_trade_events(f)
    assert (m.status, c.status) == (TradeState.MATCHED, TradeState.CONFIRMED)
    proc.on_trade(m, "c")
    proc.on_trade(c, "c")
    proc.on_trade(m, "c")  # replay is idempotent
    pos = store.position(TOK)
    assert pos.size == 40 and pos.avg_price == 0.50
    assert len(seen) == 1 and store.inflight(TOK) == 0
    store.close()
