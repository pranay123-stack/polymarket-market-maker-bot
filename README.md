# poly-maker

**Author: Pranay Gaurav**

A maker-only market-making bot for **Polymarket CLOB V2**, focused on political
markets. Single async process, local-file config, typed and tested, with a
paper-trading mode that simulates fills from **real Polymarket trade prints**.

> [!WARNING]
> Market making on Polymarket is competitive and can lose money. This is a
> research implementation, not a guaranteed-profitable product. Run `--paper`
> first; go live with small size.

## Paper trading results (real Polymarket data)

Session on **2026-11-10, 22:55 → 22:59 UTC**: 8 of the most-traded political
markets, live market WebSocket, no wallet, no orders posted. A resting paper
quote fills only when a **real** trade print on Polymarket reaches it (see
[Paper fill model](#paper-fill-model)). Full report: [`docs/paper_session.md`](docs/paper_session.md).

| metric | value |
|---|---|
| Markets quoted | 8 |
| Real trade prints on those markets | 4 |
| Paper quotes sent (post-only) | 34 |
| Fills | 1 |
| Mark-to-mid PnL | **+$0.02** |

**Trades**

| time (UTC) | market | outcome | side | price | shares | notional $ | mid at fill | mid at end |
|---|---|---|---|---:|---:|---:|---:|---:|
| 2026-11-10 22:56:38 | Will Puma Shen win the next Taipei Mayor election? | YES | BUY | 0.346 | 7.95 | 2.75 | 0.3485 | 0.3480 |

**PnL by market** (positions marked to the real Polymarket mid at session end)

| market | outcome | fills | net shares | cash $ | end mid | MTM PnL $ |
|---|---|---:|---:|---:|---:|---:|
| Will Puma Shen win the next Taipei Mayor election? | YES | 1 | 7.95 | −2.75 | 0.3480 | +0.02 |
| **Total** | | 1 | | | | **+0.02** |

The fill was a real 22.1-share SELL print at exactly 0.346. The bot's bid was
queued behind ~14 shares at that level, so it received the remaining 7.95. It
bought 0.25¢ under mid, and then posted a 0.351 offer to exit the inventory.

**Read this as a working-pipeline demonstration, not evidence of edge:** it is
one fill over a ~4-minute window. Reproduce a longer session with the commands
below and regenerate the report.

## What it does

- Discovers political markets via the **Gamma API** and ranks them by reward +
  rebate income vs. volatility/spread risk.
- Maintains a live order book per token from the **market WebSocket**.
- Quotes **maker-only**: every order is post-only. Fair-value + inventory-skew
  strategy that posts BUY-YES and BUY-NO as a two-sided quote, with live
  volatility/toxicity estimation and a regime machine that pulls quotes during
  news events.
- Reconciles a target quote set against live orders with churn tolerances; runs
  the exchange **heartbeat** dead-man switch; enforces risk caps and a daily-loss
  kill switch.
- Config, market selection and state are **local files + SQLite**.

## Install

Uses [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync --extra dev
uv run polymaker --help
```

## Configure

```bash
cp .env.example .env         # live trading only
```

- `PK`: the private key of your signer wallet
- `BROWSER_ADDRESS`: your Polymarket address

Everything else is TOML:

- `config/`: live settings (`config.toml`, `strategy.toml`, `markets.toml`)
- `papercfg/`: paper settings, with a separate `paper_state.db`, journal and logs so
  paper runs never touch live state

## Use

```bash
# 1. discover + rank political markets
uv run polymaker scan --config-dir papercfg
uv run polymaker markets --config-dir papercfg

# 2. paper trade on the live feed (no wallet needed, nothing posted)
uv run polymaker run --paper --config-dir papercfg

# 3. inspect what happened
uv run polymaker trades --config-dir papercfg     # every fill, with market + outcome
uv run polymaker pnl --config-dir papercfg        # engine equity / net cash / inventory
uv run python scripts/paper_report.py --config-dir papercfg > docs/paper_session.md

# live
uv run polymaker doctor        # preflight the wallet
uv run polymaker livetest      # deep post-only order + cancel (no fill)
uv run polymaker run           # go live
uv run polymaker status        # positions / open orders
uv run polymaker cancel-all    # panic button
```

## Paper fill model

`src/polymaker/paper/fills.py` decides when a resting paper order would have
filled, using only real `last_trade_price` prints:

- A resting BUY can be hit only by an aggressive SELL on the **same token**, and
  a resting SELL can be lifted only by an aggressive BUY.
- **Trade-through** (a print better than our price): fills up to the print size.
- **Trade at our price**: the print first consumes the size that was queued
  ahead of us when we joined the level. Only the excess fills us.
  Cancellations ahead of us are ignored, so the model can under-fill but never
  over-fill.
- Prints older than the order never fill it.
- Paper fills run through the same `MATCHED → CONFIRMED` lifecycle code as live
  user-WebSocket fills, so positions, PnL, markout and risk are computed by the
  production path.

Not modelled: YES/NO mint matches (a YES buyer matching a NO bid), maker
rebates, liquidity rewards, and latency. The first three would make live income
**higher** than simulated.

## Architecture

```
market WS ─▶ OrderBook ─▶ (wake) ─▶ Quoter ─▶ strategy (pure) ─▶ reconcile ─▶ ExecutionGateway
user WS   ─▶ StateStore                                         RiskManager ┘   (post-only, heartbeat)
Gamma     ─▶ Catalog/scanner ─▶ SQLite            periodic REST reconcile ┘
paper:  trade prints ─▶ PaperFillSimulator ─▶ same fill lifecycle as user WS
```

One async event loop. The strategy is a pure function
`(book, inventory, params, clock) → TargetQuotes`, deterministic and unit-tested.
The `ExecutionGateway` wraps `py-clob-client-v2` (V2 EIP-712 signing) and
offloads blocking calls to a thread pool. State (positions, orders, fills, PnL,
catalog) lives in one SQLite file. Raw WS and order events are journaled to
`journal/` for replay.

## Strategy

- **Fair value**: depth-weighted microprice, nudged by an EWMA of signed trade flow.
- **Quotes**: reservation price `r = FV − skew(inventory)`; half-spread
  `δ = base + c_vol·σ + c_tox·toxicity`. Post **BUY-YES at `r − δ`** and
  **BUY-NO at `(1 − r) − δ`**. Both legs are bids summing below 1, so a filled
  pair merges back to USDC at locked edge `1 − p − q`.
- **Inventory skew**: net position leans both quotes. Size tapers toward a soft
  cap, then the adding side is pulled.
- **Volatility / toxicity**: realized-vol and per-fill markout EWMAs widen the
  spread and cut size in markets that pick us off.
- **Regime machine**: `QUIET`, `TRENDING`, `EVENT` (pull quotes),
  `REDUCE_ONLY`, `HALTED`.
- **Risk**: per-market notional cap, neg-risk event-group cap, total exposure
  cap, daily-loss kill switch, WS-staleness halt.

## Develop

```bash
uv run pytest                 # 119 offline tests
POLYMAKER_LIVE=1 uv run pytest tests/test_live_marketdata.py   # live WS integration
uv run ruff check src tests scripts
uv run mypy src               # strict
```

## License

MIT, see [LICENSE](LICENSE).
