"""
业绩报告：按 config.json 里配置的期限统计模拟盘收益，并与同期「买入持有」基准对比。

用法：
    python report.py                 # 生成 reports/YYYY-MM-DD.md，并打印摘要
    python report.py --json          # 只打印 JSON（供自动化消费）
    python report.py --mode paper    # 默认就是 paper；dry 只会得到空账户

设计原则：
1. **收益率用 Alpaca 真实权益算**，不用本地估算 —— 下单滑点、成交价都体现在权益里。
2. **必须给基准**。只报自己的收益是自欺欺人；同期 SPY 买入持有是唯一诚实的对照组。
3. **没有交易就直说没有交易**，不编造"观察池正在酝酿"之类的话术。
4. 净值曲线同时给**最大回撤** —— 这套体系的核心卖点是风险调整后收益，不看回撤等于没看。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rules                      # noqa: E402
from alpaca_io import MarketData, PaperBroker   # noqa: E402

CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
MANDATE = CFG.get("mandate", {})
REPORT_DIR = HERE / "reports"
STATE_FILE = HERE / "state" / "portfolio.json"
LEDGER_DIR = HERE / "ledger"
TRADES_FILE = LEDGER_DIR / "trades.jsonl"


def pct(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:+.{digits}f}%"


def money(x: float | None) -> str:
    if x is None:
        return "—"
    return f"${x:,.2f}"


def load_local_state() -> dict:
    if not STATE_FILE.exists():
        return {"positions": {}, "pending": {}, "cycles": {}, "history": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"positions": {}, "pending": {}, "cycles": {}, "history": []}


def read_decisions(since: str, until: str) -> list[dict]:
    """读本地决策流水（trades.jsonl），按日期区间过滤。"""
    if not TRADES_FILE.exists():
        return []
    out = []
    for line in TRADES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        d = (r.get("ts") or "")[:10]
        if since <= d <= until:
            out.append(r)
    return out


def benchmark_return(symbol: str, start: str, feed: str) -> dict | None:
    """同期买入持有收益：取起始日（或其后第一个交易日）收盘 → 最新收盘。"""
    try:
        bars = MarketData(feed).bars(symbol, "day")
    except Exception as e:                                       # noqa: BLE001
        return {"symbol": symbol, "error": str(e)}
    if not bars:
        return None
    # 起始日（或其后第一个交易日）作为基准点。
    # ⚠️ 不能回退到 bars[0] —— 那是数据窗口最老的一根（约 400 天前），
    # 会把"期限刚开始、基准应为 0%"算成整段窗口的涨幅。
    cands = [b for b in bars if b["t"] >= start]
    base_bar = cands[0] if cands else bars[-1]
    base = base_bar["c"]
    last = bars[-1]["c"]
    return {
        "symbol": symbol,
        "start_date": base_bar["t"],
        "last_date": bars[-1]["t"],
        "start": base, "last": last,
        "pct": (last / base - 1.0) * 100.0 if base else None,
    }


def max_drawdown(points: list[dict]) -> float | None:
    """净值曲线的最大回撤（正数表示回撤幅度，%）。"""
    peak, mdd = None, 0.0
    for p in points:
        e = p.get("equity")
        if e is None:
            continue
        peak = e if peak is None else max(peak, e)
        if peak:
            mdd = max(mdd, (peak - e) / peak * 100.0)
    return mdd if peak else None


def build(mode: str = "paper", period: str = "1M") -> dict:
    start = MANDATE.get("start", dt.date.today().isoformat())
    end = MANDATE.get("end", start)
    today = dt.date.today().isoformat()
    initial = float(MANDATE.get("initial_equity_usd", 100000.0))
    bench_sym = MANDATE.get("benchmark", "SPY")
    feed = CFG.get("data", {}).get("feed", "iex")

    broker = PaperBroker(mode)
    acct = broker.account()
    positions = broker.positions() if mode != "dry" else {}
    hist = broker.portfolio_history(period=period) if mode != "dry" else {"base_value": 0, "points": []}
    orders = broker.orders() if mode != "dry" else []

    equity = acct.get("equity")
    ret_pct = ((equity / initial - 1.0) * 100.0) if (equity and initial) else None

    # 期限进度
    try:
        d0 = dt.date.fromisoformat(start)
        d1 = dt.date.fromisoformat(end)
        total_days = (d1 - d0).days
        elapsed = (dt.date.fromisoformat(today) - d0).days
    except Exception:                                            # noqa: BLE001
        total_days = elapsed = 0

    bench = benchmark_return(bench_sym, start, feed)
    bench_pct = bench.get("pct") if bench else None
    excess = (ret_pct - bench_pct) if (ret_pct is not None and bench_pct is not None) else None

    # 持仓明细（合并 Alpaca 实际持仓 + 本地止损线）
    local = load_local_state()
    r_cfg = CFG["rules"]
    holdings = []
    for sym, p in sorted(positions.items()):
        lp = local.get("positions", {}).get(sym, {})
        entry = lp.get("entry_price") or p.get("avg_entry_price")
        price = (p["market_value"] / p["qty"]) if p.get("qty") else None
        si = rules.stop_line(entry, price, r_cfg) if (entry and price) else None
        holdings.append({
            "symbol": sym,
            "qty": p.get("qty"),
            "entry": entry,
            "price": price,
            "market_value": p.get("market_value"),
            "unrealized_pct": (p.get("unrealized_plpc") or 0) * 100.0,
            "stop_line": si.line if si else None,
            "stop_label": si.label if si else "—",
            "to_stop_pct": ((price / si.line - 1) * 100.0) if (si and price and si.line) else None,
        })

    # 本期决策统计
    decisions = read_decisions(start, today)
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.get("type", "?")] = counts.get(d.get("type", "?"), 0) + 1

    return {
        "today": today,
        "mandate": {"start": start, "end": end,
                    "elapsed_days": elapsed, "total_days": total_days,
                    "initial_equity": initial},
        "account": {"equity": equity, "cash": acct.get("cash"),
                    "buying_power": acct.get("buying_power"),
                    "mode": mode},
        "return_pct": ret_pct,
        "pnl_usd": (equity - initial) if equity else None,
        "benchmark": bench,
        "excess_pct": excess,
        "equity_curve": hist.get("points", []),
        "curve_base_value": hist.get("base_value"),
        "max_drawdown_pct": max_drawdown(hist.get("points", [])),
        "holdings": holdings,
        "orders": orders,
        "decision_counts": counts,
        "decision_total": len(decisions),
        "pending": local.get("pending", {}),
    }


def render_md(d: dict) -> str:
    L: list[str] = []
    m, a = d["mandate"], d["account"]
    L.append(f"# 模拟盘业绩报告 · {d['today']}\n")
    L.append(f"**期限**：{m['start']} → {m['end']}"
             f"（已进行 {m['elapsed_days']} 天 / 共 {m['total_days']} 天）\n")

    L.append("## 一、收益\n")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 初始权益 | {money(m['initial_equity'])} |")
    L.append(f"| 当前权益 | {money(a['equity'])} |")
    L.append(f"| 累计盈亏 | {money(d['pnl_usd'])} |")
    L.append(f"| **本体系收益率** | **{pct(d['return_pct'])}** |")
    b = d.get("benchmark") or {}
    if b and b.get("pct") is not None:
        L.append(f"| 同期 {b['symbol']} 买入持有 | {pct(b['pct'])}"
                 f"（{b['start_date']} → {b['last_date']}） |")
        L.append(f"| **相对基准超额** | **{pct(d['excess_pct'])}** |")
    else:
        L.append(f"| 同期基准 | 取数失败（{b.get('error', '未知原因')}） |")
    if d.get("max_drawdown_pct") is not None:
        L.append(f"| 区间最大回撤 | {d['max_drawdown_pct']:.2f}% |")
    L.append("")

    L.append("## 二、持仓\n")
    if d["holdings"]:
        L.append("| 标的 | 数量 | 成本 | 现价 | 市值 | 浮动盈亏 | 止损线 | 距止损 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for h in d["holdings"]:
            L.append(f"| {h['symbol']} | {h['qty']:.4g} | {money(h['entry'])} | "
                     f"{money(h['price'])} | {money(h['market_value'])} | "
                     f"{pct(h['unrealized_pct'])} | {money(h['stop_line'])} "
                     f"（{h['stop_label']}） | {pct(h['to_stop_pct'])} |")
    else:
        L.append("_空仓。_\n")
    L.append("")

    L.append("## 三、等待入场\n")
    if d["pending"]:
        L.append("| 标的 | 信号月 | 参考价 H | 失效日 |")
        L.append("|---|---|---|---|")
        for sym, p in d["pending"].items():
            hp = f"{p['h_price']:.2f}" if p.get("h_price") else "尚未形成"
            L.append(f"| {sym} | {p.get('signal_month', '—')} | {hp} | {p.get('deadline', '—')} |")
    else:
        L.append("_无。_\n")
    L.append("")

    L.append("## 四、本期成交\n")
    filled = [o for o in d["orders"] if (o.get("filled_qty") or 0) > 0]
    if filled:
        L.append("| 时间 | 标的 | 方向 | 数量 | 成交价 |")
        L.append("|---|---|---|---|---|")
        for o in filled:
            L.append(f"| {(o.get('submitted_at') or '')[:19]} | {o['symbol']} | "
                     f"{o['side']} | {o['filled_qty']:.4g} | {money(o.get('filled_avg_price'))} |")
    else:
        L.append("_本期无成交。_\n")
    L.append("")

    L.append("## 五、本期决策统计\n")
    if d["decision_counts"]:
        L.append("| 动作 | 次数 |")
        L.append("|---|---|")
        for k, v in sorted(d["decision_counts"].items(), key=lambda kv: -kv[1]):
            L.append(f"| {k} | {v} |")
        L.append(f"\n合计 {d['decision_total']} 条决策记录。")
    else:
        L.append("_无决策记录。_")
    L.append("")

    if d["equity_curve"]:
        L.append("## 六、净值曲线\n")
        L.append("| 日期 | 权益 | 累计 |")
        L.append("|---|---|---|")
        for p in d["equity_curve"][-20:]:
            L.append(f"| {p['t']} | {money(p['equity'])} | {pct(p.get('plpc'))} |")
        L.append("")

    L.append("---\n")
    L.append("*数据来源：Alpaca 模拟盘（paper）账户权益。基准为同期买入持有。*")
    L.append("*体系方法来源：HOLDLE 公开课程，版权归原作者所有。**不构成投资建议**。*")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟盘业绩报告")
    ap.add_argument("--mode", default="paper", choices=["paper", "dry"])
    ap.add_argument("--period", default="1M", help="净值曲线区间：1W/1M/3M/1A/all")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    d = build(mode=args.mode, period=args.period)

    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{d['today']}.md"
    path.write_text(render_md(d), encoding="utf-8")

    a = d["account"]
    print(f"── 模拟盘业绩 · {d['today']} ──")
    print(f"  期限      : {d['mandate']['start']} → {d['mandate']['end']}"
          f"（已进行 {d['mandate']['elapsed_days']}/{d['mandate']['total_days']} 天）")
    print(f"  权益      : {money(a['equity'])}"
          f"（初始 {money(d['mandate']['initial_equity'])}）")
    print(f"  收益率    : {pct(d['return_pct'])}   盈亏 {money(d['pnl_usd'])}")
    b = d.get("benchmark") or {}
    if b.get("pct") is not None:
        print(f"  基准 {b['symbol']:<4}  : {pct(b['pct'])}   超额 {pct(d['excess_pct'])}")
    else:
        print(f"  基准      : 取数失败（{b.get('error', '未知')}）")
    if d.get("max_drawdown_pct") is not None:
        print(f"  最大回撤  : {d['max_drawdown_pct']:.2f}%")
    print(f"  持仓      : {len(d['holdings'])} 只"
          + ("（空仓）" if not d["holdings"] else ""))
    print(f"  等待入场  : {', '.join(d['pending'].keys()) or '无'}")
    print(f"  本期成交  : {len([o for o in d['orders'] if (o.get('filled_qty') or 0) > 0])} 笔")
    print(f"\n报告已写入：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
