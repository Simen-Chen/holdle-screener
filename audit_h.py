"""
参考价 H 复核工具。

为什么需要它：H 是整套系统里**唯一一个决定"什么时候买"的数字**。
H 定高了 → 永远不触发；H 定低了 → 提前买入，等于放弃"等好"。
而 `find_reference_high` 里有一个已知的口径选择（入场窗口内最早 5 根K线不参与
候选），它会让 H 落在"第二个高点"而不是"第一个高点"上。这个选择对结果影响很大，
但平时完全看不出来 —— 报告上只显示一个数字。

本工具把入场窗口里的**每一根**日K都列出来，标出：
  · 哪些是局部高点（候选）
  · 哪些被 `start_idx + window` 的前置条件跳过了
  · 每根的日K MACD 柱（正/负）
  · 回撤段有没有出绿柱（H 成立的硬条件）
  · 最终 H 落在哪一根

用法：
    python audit_h.py                # 复核所有「等待入场」的标的
    python audit_h.py JPM,BAC        # 只看指定标的
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engine                                          # noqa: E402
import rules                                           # noqa: E402
from alpaca_io import MarketData                       # noqa: E402


def audit(sym: str, cfg: dict, md: MarketData, today: str) -> None:
    monthly = md.bars(sym, "month")
    weekly = md.bars(sym, "week")
    daily = md.bars(sym, "day")

    sig = engine.monthly_signal({"symbol": sym}, monthly, cfg, today)
    print("=" * 78)
    print(f"{sym}  信号月 {sig['signal_month']}  场景 {sig['scenario']}")
    print(f"  月线：DIF {sig['dif']:.2f} / DEA {sig['dea']:.2f} / "
          f"柱 {sig['hist']:+.3f}（上月 {sig['hist_prev']:+.3f}）")
    print(f"  前置校验：{'✅' if sig['pre_ok'] else '❌'}  {sig['pre_detail']}")
    print(f"  闸门：{'🟢 开' if sig['gate_ok'] else '⛔ ' + sig['gate_reason']}")

    signal_month = sig["signal_month"]
    if not signal_month:
        print("  （无信号月，无法复核 H）")
        return

    start_idx = next((i for i, b in enumerate(daily)
                      if b["t"][:7] > signal_month), None)
    if start_idx is None:
        print(f"  （信号月 {signal_month} 之后还没有日K数据）")
        return

    d_close = [b["c"] for b in daily]
    _, _, d_hist = rules.macd(d_close, cfg["rules"]["macd_fast"],
                              cfg["rules"]["macd_slow"], cfg["rules"]["macd_signal"])
    W = 5
    h_idx, h_price, h_detail = rules.find_reference_high(
        [b["h"] for b in daily], [b["l"] for b in daily], d_hist, start_idx)

    print(f"  入场窗口起点：{daily[start_idx]['t']}（第 {start_idx} 根）｜ "
          f"当前窗口内 {len(daily) - start_idx} 根日K")
    print(f"  find_reference_high 从第 {start_idx + W} 根起找 → "
          f"最早可当选的日期是 "
          f"{daily[start_idx + W]['t'] if start_idx + W < len(daily) else '—'}")
    print()
    print("    #  日期        高      低      收     日K柱   局部高点  说明")
    print("  " + "-" * 76)
    for i in range(start_idx, len(daily)):
        b = daily[i]
        lo = max(0, i - W)
        hi = min(len(daily), i + W + 1)
        is_peak = b["h"] == max(x["h"] for x in daily[lo:hi])
        after_lows = [x["l"] for x in daily[i + 1:]]
        after_hist = d_hist[i + 1:]
        retreated = bool(after_lows) and min(after_lows) < b["h"]
        green = bool(after_hist) and min(after_hist) < 0
        note = []
        if i < start_idx + W:
            note.append("⚠️ 在 start_idx+window 之前，永不参选")
        if is_peak and retreated and green:
            note.append("✅ 三条件齐（峰+回撤+绿柱）")
        elif is_peak and retreated and not green:
            note.append("⛔ 回撤但未出绿柱")
        elif is_peak and not retreated:
            note.append("⛔ 未回撤")
        mark = "◀ H" if i == h_idx else ""
        print(f"  {i:>4}  {b['t']}  {b['h']:>7.2f} {b['l']:>7.2f} "
              f"{b['c']:>7.2f} {d_hist[i]:>+7.3f}  "
              f"{'是' if is_peak else '  '}      "
              f"{' '.join(note)} {mark}")
    print()
    if h_price:
        print(f"  ⇒ H = {h_price:.2f}（{daily[h_idx]['t']}）｜ 现价 {daily[-1]['c']:.2f} ｜ "
              f"距 H {(daily[-1]['c'] / h_price - 1) * 100:+.2f}%")
        first_peak = next((i for i in range(start_idx, len(daily))
                           if daily[i]["h"] == max(x["h"] for x in daily[max(0, i - W):i + W + 1])),
                          None)
        if first_peak is not None and first_peak != h_idx:
            print(f"  ⚠️ 口径提示：窗口内**第一个**局部高点是 "
                  f"{daily[first_peak]['h']:.2f}（{daily[first_peak]['t']}，第 {first_peak} 根），"
                  f"但当前实现取的是 {daily[h_idx]['t']} 的 {h_price:.2f}。")
            print(f"     若改成「取第一个确认高点」，H 会变成 "
                  f"{daily[first_peak]['h']:.2f}，"
                  f"买点{'更高更保守' if daily[first_peak]['h'] > h_price else '更低更激进'}。")
    else:
        print(f"  ⇒ H 尚未形成：{h_detail}")


def main() -> None:
    ap = argparse.ArgumentParser(description="复核等待入场标的的参考价 H")
    ap.add_argument("symbols", nargs="?", default=None,
                    help="逗号分隔；缺省=state 里所有等待入场的标的")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    args = ap.parse_args()

    import json
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    today = dt.date.today().isoformat()

    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        syms = list(engine.load_state()["pending"].keys())
    if not syms:
        print("没有等待入场的标的。")
        return

    md = MarketData(cfg["data"]["feed"])
    print(f"\n参考价 H 复核 ｜ {today} ｜ 共 {len(syms)} 只：{'、'.join(syms)}\n")
    for s in syms:
        audit(s, cfg, md, today)
    print("\n注：本工具只读，不改任何状态。窗口宽度 window=5（左右各 5 根）。")


if __name__ == "__main__":
    main()
