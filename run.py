"""
CLI 入口。

    python run.py scan            扫描观察池，出决策与计划（不下单）
    python run.py run             按 config.mode 执行（默认 dry）
    python run.py run --mode paper  连 Alpaca 模拟盘真实下单
    python run.py status          查看当前持仓 / 等待入场 / 历史
    python run.py selftest        跑规则自检（不需要网络和密钥）

可选：
    --symbols V,MSFT    只跑指定标的
    --config other.json 指定配置文件
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engine                                    # noqa: E402
from alpaca_io import MarketData, PaperBroker, has_keys   # noqa: E402


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"找不到配置文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_selftest() -> None:
    import rules
    rules._selftest()


def cmd_status() -> None:
    state = engine.load_state()
    print("=" * 62)
    print("当前持仓")
    print("=" * 62)
    if not state["positions"]:
        print("  （空仓）")
    for sym, p in state["positions"].items():
        print(f"  {sym:6s} {p['qty']:>8.0f} 股 ｜ 入场 {p['entry_price']:.2f} "
              f"({p['entry_date']}) ｜ {p.get('stop_label','')} 线 {p.get('stop_line',0):.2f} "
              f"｜ {p['sector']}")
    print("\n" + "=" * 62)
    print("等待入场")
    print("=" * 62)
    if not state["pending"]:
        print("  （无）")
    for sym, p in state["pending"].items():
        hp = f"{p['h_price']:.2f}" if p.get("h_price") else "待形成"
        print(f"  {sym:6s} 信号月 {p.get('signal_month')} ｜ 参考价 {hp} ｜ "
              f"失效 {p.get('deadline')} ｜ 重入={p.get('is_reentry')}")
    print("\n" + "=" * 62)
    print(f"历史动作：{len(state['history'])} 条")
    print("=" * 62)
    for h in state["history"][-12:]:
        print(f"  {h.get('ts','')[:16]}  {h['symbol']:6s} {h['type']:7s} {h.get('reason','')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="HOLDLE 体系 · Alpaca 模拟盘自动交易")
    ap.add_argument("cmd", nargs="?", default="scan",
                    choices=["scan", "run", "status", "selftest"])
    ap.add_argument("--mode", choices=["dry", "paper"], default=None)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    args = ap.parse_args()

    if args.cmd == "selftest":
        cmd_selftest()
        return
    if args.cmd == "status":
        cmd_status()
        return

    cfg = load_config(Path(args.config))
    today = dt.date.today().isoformat()

    mode = "dry" if args.cmd == "scan" else (args.mode or cfg.get("mode", "dry"))
    if mode == "paper" and not has_keys():
        sys.exit("paper 模式需要 ALPACA_API_KEY / ALPACA_SECRET_KEY 环境变量。\n"
                 "先跑 `python run.py scan` 做 dry-run。")

    if args.symbols:
        keep = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        cfg["watchlist"] = [w for w in cfg["watchlist"] if w["symbol"] in keep]

    print(f"── HOLDLE 体系扫描 ｜ {today} ｜ 模式 {mode} ｜ "
          f"观察池 {len(cfg['watchlist'])} 只 ──\n")

    md = MarketData(cfg["data"]["feed"])
    broker = PaperBroker(mode)
    state = engine.load_state()

    rows: list[dict] = []
    records: list[dict] = []

    for item in cfg["watchlist"]:
        sym = item["symbol"]
        try:
            monthly = md.bars(sym, "month")
            weekly = md.bars(sym, "week")
            daily = md.bars(sym, "day")
        except Exception as e:                                   # noqa: BLE001
            print(f"  ⚠️  {sym:6s} 行情获取失败：{e}")
            rows.append({"symbol": sym, "name": item.get("name", sym),
                         "sector": item.get("sector", "—"), "bars_ok": False,
                         "data_ok": False, "data_note": f"行情获取失败：{e}",
                         "price": 0.0, "dif": 0, "dea": 0, "hist": 0, "hist_prev": 0,
                         "state_a": False, "pre_ok": False, "pre_detail": "",
                         "scenario": None, "scenario_detail": "", "signal_month": None,
                         "window_open": False, "contracting": False,
                         "gate_ok": False, "gate_reason": "行情获取失败",
                         "weekly_ref": None, "weekly_seq": [],
                         "h_price": None, "h_date": None, "h_detail": "",
                         "h_broken_date": None,
                         "deadline": None, "today": today})
            continue

        row = engine.analyze(item, monthly, weekly, daily, cfg, today)
        rows.append(row)
        acts = engine.decide(row, cfg, state)
        recs = engine.execute(acts, row, broker, cfg, state, today)
        records.extend(recs)

        flag = "✅" if row["state_a"] else "  "
        if not row.get("data_ok", True):
            flag = "⛔"
        scen = row["scenario"] or ""
        act_str = ", ".join(r["type"] for r in recs) or "—"
        print(f"  {flag} {sym:6s} {row['price']:>9.2f}  柱 {row['hist']:+9.2f}  "
              f"{scen:<14s} {act_str}")

    engine.save_state(state)
    path = engine.write_ledger(rows, records, cfg, mode, today)

    print(f"\n── 本次动作 {len(records)} 条 ──")
    for r in records:
        print(f"  {r['type']:7s} {r['symbol']:6s} {r.get('reason','')}")
    print(f"\n操作记录：{path}")
    print(f"持仓状态：{engine.STATE_FILE}")
    if mode == "dry":
        print("\n（dry-run：以上仅为决策与计划，未向 Alpaca 提交任何委托）")


if __name__ == "__main__":
    main()
