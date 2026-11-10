"""Build a Markdown report of a paper session from its SQLite DB + WS journal.

    uv run python scripts/paper_report.py --config-dir papercfg > docs/paper_session.md

Everything comes from what the engine recorded: fills from the `fills` table,
the bot's own equity snapshots from `pnl_snapshots`, and real Polymarket
best-bid/ask + trade prints from the journal (used to mark positions to mid).
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from polymaker.config import Config


def _t(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S")


def _mid(bb: str | None, ba: str | None) -> float | None:
    try:
        b, a = float(bb), float(ba)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return (b + a) / 2 if 0 < b < a < 1 else None


def load_journal(path: Path) -> tuple[dict[str, list[tuple[float, float]]], dict[str, int], int, float, float]:
    """-> (mids per token as sorted (ts, mid), trade prints per token, quotes sent, t0, t1)."""
    mids: dict[str, list[tuple[float, float]]] = defaultdict(list)
    prints: dict[str, int] = defaultdict(int)
    quotes = 0
    t0, t1 = float("inf"), 0.0
    for line in path.open():
        rec = json.loads(line)
        ts, kind, data = rec["ts"], rec["kind"], rec["data"]
        t0, t1 = min(t0, ts), max(t1, ts)
        if kind == "orders_out":
            quotes += len(data)
        elif kind == "price_change":
            for ch in data.get("price_changes", []):
                m = _mid(ch.get("best_bid"), ch.get("best_ask"))
                if m is not None:
                    mids[ch["asset_id"]].append((ts, m))
        elif kind == "book":
            bids, asks = data.get("bids") or [], data.get("asks") or []
            if bids and asks:
                bb = max(float(x["price"]) for x in bids)
                ba = min(float(x["price"]) for x in asks)
                m = _mid(str(bb), str(ba))
                if m is not None:
                    mids[data["asset_id"]].append((ts, m))
        elif kind == "last_trade_price":
            prints[data.get("asset_id", "")] += 1
    for v in mids.values():
        v.sort()
    return mids, prints, quotes, t0, t1


def mid_at(series: list[tuple[float, float]], ts: float) -> float | None:
    i = bisect.bisect_right(series, (ts, float("inf"))) - 1
    return series[i][1] if i >= 0 else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", default="papercfg")
    args = ap.parse_args()
    cfg = Config.load(args.config_dir)

    conn = sqlite3.connect(cfg.paths.db)
    conn.row_factory = sqlite3.Row
    names: dict[str, tuple[str, str]] = {}
    for m in conn.execute("SELECT question, meta_json FROM markets"):
        meta = json.loads(m["meta_json"])
        for tk in meta.get("tokens", []):
            names[tk["token_id"]] = (m["question"] or "", str(tk.get("outcome", "?")).upper())
    fills = conn.execute("SELECT * FROM fills ORDER BY ts").fetchall()
    snaps = conn.execute("SELECT * FROM pnl_snapshots ORDER BY ts").fetchall()
    conn.close()

    journal = Path(cfg.paths.journal_dir) / "paper.jsonl"
    mids, prints, quotes, t0, t1 = load_journal(journal)
    traded_tokens = {t for t in names if t in mids or t in prints}
    our_prints = sum(n for t, n in prints.items() if t in names)

    out: list[str] = []
    w = out.append
    w("# Paper session report\n")
    w(f"- **Window:** {_t(t0)} → {_t(t1)} UTC ({(t1 - t0) / 3600:.2f} h)")
    w(f"- **Markets quoted:** {len({names[t][0] for t in traded_tokens})}")
    w(f"- **Real trade prints seen on those markets:** {our_prints:,}")
    w(f"- **Quotes sent (post-only, paper):** {quotes:,}")
    w(f"- **Fills:** {len(fills)}\n")

    w("## Fills\n")
    if not fills:
        w("_No fills: no real trade reached the bot's resting quotes in this window._\n")
    else:
        w("| time (UTC) | market | outcome | side | price | shares | notional $ | mid at fill | mid at end |")
        w("|---|---|---|---|---:|---:|---:|---:|---:|")
        for f in fills:
            q, leg = names.get(f["token_id"], (f["token_id"][:12], "?"))
            s = mids.get(f["token_id"], [])
            mf, me = mid_at(s, f["ts"]), (s[-1][1] if s else None)
            w(f"| {_t(f['ts'])} | {q[:55]} | {leg} | {f['side']} | {f['price']:.3f} | "
              f"{f['size']:.2f} | {f['price'] * f['size']:.2f} | "
              f"{'' if mf is None else f'{mf:.4f}'} | {'' if me is None else f'{me:.4f}'} |")
        w("")

    # per-market mark-to-mid PnL, rebuilt independently from the fills
    per: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for f in fills:
        p = per[f["token_id"]]
        sign = 1 if f["side"] == "BUY" else -1
        p["shares"] += sign * f["size"]
        p["cash"] -= sign * f["price"] * f["size"]
        p["n"] += 1
    w("## PnL by market (marked to the real Polymarket mid at session end)\n")
    total = 0.0
    if per:
        w("| market | outcome | fills | net shares | cash $ | end mid | MTM PnL $ |")
        w("|---|---|---:|---:|---:|---:|---:|")
        for tok, p in sorted(per.items(), key=lambda kv: names.get(kv[0], ("", ""))):
            q, leg = names.get(tok, (tok[:12], "?"))
            s = mids.get(tok, [])
            me = s[-1][1] if s else 0.0
            pnl = p["cash"] + p["shares"] * me
            total += pnl
            w(f"| {q[:55]} | {leg} | {int(p['n'])} | {p['shares']:.2f} | {p['cash']:.2f} | "
              f"{me:.4f} | {pnl:+.2f} |")
        w(f"| **Total** | | {len(fills)} | | | | **{total:+.2f}** |\n")
    else:
        w("_No positions._\n")

    w("## Engine's own equity snapshots\n")
    if snaps:
        last = snaps[-1]
        w(f"Last snapshot {_t(last['ts'])} UTC: equity **{last['equity']:+.2f}**, "
          f"net cash {last['net_cash']:+.2f}, inventory {last['inventory_value']:.2f} "
          "(inventory marked at the engine's fair value).\n")
        w("| time (UTC) | equity $ | net cash $ | inventory $ |")
        w("|---|---:|---:|---:|")
        step = max(1, len(snaps) // 12)
        for r in list(snaps[::step]) + ([snaps[-1]] if (len(snaps) - 1) % step else []):
            w(f"| {_t(r['ts'])} | {r['equity']:+.2f} | {r['net_cash']:+.2f} | {r['inventory_value']:.2f} |")
    else:
        w("_No snapshots recorded._")
    print("\n".join(out))


if __name__ == "__main__":
    main()
