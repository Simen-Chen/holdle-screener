"""
离线全链路演练（不需要网络、不需要密钥）。

Part 1  决策真值表：直接构造 row/state，验证 decide() 在每种场景下的动作
Part 2  合成行情全链路：analyze → decide → execute → write_ledger 不报错
Part 3  状态持久化：跑两轮，确认 pending 被正确保存与推进

    python simtest.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engine          # noqa: E402
import rules           # noqa: E402
from alpaca_io import PaperBroker   # noqa: E402

CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
CFG["account"]["dry_equity_usd"] = 100000.0
TODAY = "2026-09-15"

OK = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


def make_row(**kw) -> dict:
    base = dict(
        symbol="TST", name="测试标的", sector="信息技术", price=100.0, today=TODAY,
        dif=1.0, dea=0.5, hist=0.2, hist_prev=-0.1,
        state_a=True, pre_ok=True, pre_detail="",
        scenario=rules.SCENARIO1, scenario_detail="", signal_month="2026-08",
        window_open=True, contracting=False,
        gate_ok=True, gate_reason="",
        weekly_ref=None, weekly_seq=[], h_price=None, h_date=None,
        h_detail="", h_broken_date=None, deadline=None, bars_ok=True,
    )
    base.update(kw)
    return base


def fresh_state() -> dict:
    return engine.empty_state()


def types_of(acts: list[dict]) -> set[str]:
    return {a["type"] for a in acts}


# ------------------------------------------------------------------ Part 1

def part1() -> None:
    print("── Part 1 · 决策真值表 ──")

    # 1 新信号 → ARM
    a = engine.decide(make_row(), CFG, fresh_state())
    check("新信号（场景一）→ ARM", types_of(a) == {"ARM"}, a)

    # 2 行业已满 → SKIP
    st = fresh_state()
    st["positions"] = {
        "A1": {"qty": 1, "entry_price": 1, "sector": "信息技术"},
        "A2": {"qty": 1, "entry_price": 1, "sector": "信息技术"},
    }
    a = engine.decide(make_row(), CFG, st)
    check("行业已满（信息技术 2 只）→ SKIP", types_of(a) == {"SKIP"}, a)

    # 3 pending + 突破 → BUY
    st = fresh_state()
    st["pending"] = {"TST": {"signal_month": "2026-08", "h_price": 95.0,
                             "deadline": "2026-12-01"}}
    a = engine.decide(make_row(price=100.0), CFG, st)
    check("等待中 + 突破参考价 → BUY", types_of(a) == {"BUY"}, a)

    # 4 pending + 未突破 → WAIT
    a = engine.decide(make_row(price=90.0), CFG, st)
    check("等待中 + 未突破 → WAIT", types_of(a) == {"WAIT"}, a)

    # 5 pending + 过期 → CANCEL
    st2 = fresh_state()
    st2["pending"] = {"TST": {"signal_month": "2026-01", "h_price": 95.0,
                              "deadline": "2026-03-01"}}
    a = engine.decide(make_row(price=90.0), CFG, st2)
    check("等待中 + 超过失效日 → CANCEL", types_of(a) == {"CANCEL"}, a)

    # 6 持仓 + 未触止损 → HOLD
    st = fresh_state()
    st["positions"] = {"TST": {"qty": 10, "entry_price": 100.0, "sector": "信息技术"}}
    a = engine.decide(make_row(price=100.0), CFG, st)
    check("持仓 + 未触止损 → HOLD", types_of(a) == {"HOLD"}, a)

    # 7 持仓 + 跌破 −20% → SELL
    a = engine.decide(make_row(price=79.0), CFG, st)
    check("持仓 + 跌破 P×0.80 → SELL", types_of(a) == {"SELL"}, a)

    # 8 持仓 + 涨 25% → HOLD，止损线上移到 P×0.90
    st3 = fresh_state()
    st3["positions"] = {"TST": {"qty": 10, "entry_price": 100.0, "sector": "信息技术"}}
    a = engine.decide(make_row(price=125.0), CFG, st3)
    check("涨 25% → HOLD 且止损线上移", types_of(a) == {"HOLD"}
          and abs(st3["positions"]["TST"]["stop_line"] - 90.0) < 1e-6,
          st3["positions"]["TST"])

    # 9 持仓 + 跌破周K参考价 → SELL（周K规则优先）
    a = engine.decide(make_row(price=110.0, weekly_ref=120.0), CFG, st3)
    check("持仓 + 跌破周K有效低点 → SELL", types_of(a) == {"SELL"}
          and "周K" in a[0]["reason"], a)

    # 10 场景二 + 无止损记录 → SKIP（错过初期不追）
    a = engine.decide(make_row(scenario=rules.SCENARIO2), CFG, fresh_state())
    check("场景二 + 无止损记录 → SKIP（错过初期不追）", types_of(a) == {"SKIP"}, a)

    # 11 场景二 + 有止损记录 + 价 ≤ 止损价 → SKIP（R1）
    st = fresh_state()
    st["cycles"] = {"TST": {"last_stop_price": 105.0, "reentries": 0}}
    a = engine.decide(make_row(scenario=rules.SCENARIO2, price=100.0), CFG, st)
    check("重入 + 价 ≤ 上次止损价 → SKIP（R1）", types_of(a) == {"SKIP"}, a)

    # 12 场景二 + 有止损记录 + 价 > 止损价 → ARM（重入）
    a = engine.decide(make_row(scenario=rules.SCENARIO2, price=110.0), CFG, st)
    check("重入 + 价 > 上次止损价 → ARM", types_of(a) == {"ARM"}, a)

    # 13 场景二 + 重入已满 → SKIP（R3）
    st = fresh_state()
    st["cycles"] = {"TST": {"last_stop_price": 50.0, "reentries": 2}}
    a = engine.decide(make_row(scenario=rules.SCENARIO2, price=110.0), CFG, st)
    check("重入已达 2 次 → SKIP（R3）", types_of(a) == {"SKIP"}, a)

    # 14 非状态A → 无动作
    a = engine.decide(make_row(state_a=False, scenario=None), CFG, fresh_state())
    check("非状态A → 无动作", a == [], a)

    # 15 前置校验不通过 → 无动作
    a = engine.decide(make_row(pre_ok=False, scenario=None), CFG, fresh_state())
    check("前置校验不通过 → 无动作", a == [], a)


# ------------------------------------------------------------------ Part 2

def synth_bars(n: int, start: float, end: float, tf: str,
               start_date: dt.date, step_days: int) -> list[dict]:
    """线性走势的合成 K 线，带一点正弦扰动。"""
    import math
    out = []
    d = start_date
    for i in range(n):
        p = start + (end - start) * (i / max(1, n - 1))
        p *= 1 + 0.02 * math.sin(i / 3.0)
        out.append({
            "t": d.isoformat(),
            "o": p * 0.995, "h": p * 1.01, "l": p * 0.985, "c": p, "v": 1e6,
        })
        d += dt.timedelta(days=step_days)
    return out


def part2() -> None:
    print("\n── Part 2 · 合成行情全链路 ──")
    tmp = Path(tempfile.mkdtemp(prefix="holdle_sim_"))
    engine.LEDGER_DIR = tmp
    engine.STATE_DIR = tmp
    engine.STATE_FILE = tmp / "portfolio.json"

    monthly = synth_bars(60, 50, 120, "M", dt.date(2021, 10, 1), 30)
    weekly = synth_bars(120, 80, 120, "W", dt.date(2024, 5, 6), 7)
    daily = synth_bars(260, 95, 120, "D", dt.date(2025, 9, 1), 1)

    row = engine.analyze({"symbol": "SYN", "name": "合成", "sector": "信息技术"},
                         monthly, weekly, daily, CFG, TODAY)
    check("analyze 跑通（bars_ok）", row["bars_ok"])
    check("analyze 输出关键字段", all(k in row for k in
          ("dif", "dea", "hist", "state_a", "pre_ok", "scenario", "weekly_ref")))

    state = engine.empty_state()
    broker = PaperBroker("dry")
    acts = engine.decide(row, CFG, state)
    recs = engine.execute(acts, row, broker, CFG, state, TODAY)
    check("decide + execute 跑通", isinstance(recs, list))

    path = engine.write_ledger([row], recs, CFG, "dry", TODAY)
    check("操作记录已生成", path.exists() and path.stat().st_size > 200, path)
    body = path.read_text(encoding="utf-8")
    check("记录含扫描全表", "观察池扫描全表" in body)
    check("记录含免责声明", "不构成投资建议" in body)
    check("trades.jsonl 已生成", (tmp / "trades.jsonl").exists())

    print(f"     （演练产物写入临时目录 {tmp}）")


# ------------------------------------------------------------------ Part 3

def part3() -> None:
    print("\n── Part 3 · 状态持久化 ──")
    tmp = Path(tempfile.mkdtemp(prefix="holdle_state_"))
    engine.STATE_DIR = tmp
    engine.STATE_FILE = tmp / "portfolio.json"

    st = engine.empty_state()
    row = make_row(symbol="PER", scenario=rules.SCENARIO1)
    recs = engine.execute(engine.decide(row, CFG, st), row, PaperBroker("dry"),
                          CFG, st, TODAY)
    engine.save_state(st)
    check("第一轮 ARM 后 pending 已写入", "PER" in st["pending"], st["pending"])
    check("ARM 同时初始化了 cycle", "PER" in st["cycles"], st["cycles"])

    st2 = engine.load_state()
    check("状态可回读", "PER" in st2["pending"])

    # 第二轮：价格突破参考价 → BUY
    st2["pending"]["PER"]["h_price"] = 95.0
    row2 = make_row(symbol="PER", price=101.0, scenario=rules.SCENARIO1)
    recs2 = engine.execute(engine.decide(row2, CFG, st2), row2, PaperBroker("dry"),
                           CFG, st2, TODAY)
    check("第二轮突破 → BUY", any(r["type"] == "BUY" for r in recs2), recs2)
    check("买入后进入持仓", "PER" in st2["positions"], st2["positions"])
    check("买入后 pending 已清空", "PER" not in st2["pending"])
    check("等权重额度正确（100000/5/101 ≈ 198 股）",
          st2["positions"]["PER"]["qty"] == 198,
          st2["positions"]["PER"]["qty"])

    # 第三轮：跌到 −20% 以下 → SELL，并记录 last_stop_price
    row3 = make_row(symbol="PER", price=70.0)
    recs3 = engine.execute(engine.decide(row3, CFG, st2), row3, PaperBroker("dry"),
                           CFG, st2, TODAY)
    check("第三轮跌破止损 → SELL", any(r["type"] == "SELL" for r in recs3), recs3)
    check("卖出后清仓", "PER" not in st2["positions"])
    check("已记录 last_stop_price（供 R1 用）",
          st2["cycles"]["PER"]["last_stop_price"] == 70.0, st2["cycles"]["PER"])


# ------------------------------------------------------------------ Part 4

def part4() -> None:
    """数据体检：复权失败的指纹识别 + 坏数据绝不触发交易。"""
    print("\n── Part 4 · 数据体检与坏数据熔断 ──")
    from alpaca_io import consistency_gap, split_cliffs

    def bar(t, c):
        return {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 1.0}

    # ① 拆股指纹：单日 10:1
    split_day = [bar("2026-06-01", 1800.0), bar("2026-06-02", 180.0)]
    check("日K单日 10:1 跳变 → 被识别为断崖",
          len(split_cliffs(split_day)) == 1, split_cliffs(split_day))

    # ② 真实暴跌（逐步下跌）不该被误杀
    crash = [bar(f"2026-07-{d:02d}", 300.0 - 8.0 * i)
             for i, d in enumerate(range(1, 11))]
    check("日K逐步 −24% 真实下跌 → 不误报", split_cliffs(crash) == [],
          split_cliffs(crash))

    # ③ 月线一根 −39% 是真行情：engine 只拿日K判断崖，不该误杀
    #    （KLAC 2026-07 真实案例：301.15 → 182.47 是逐步跌下来的，不是拆股）
    m = ([bar(f"2025-{i:02d}-01", 150.0 + i * 5) for i in range(1, 13)] +
         [bar("2026-01-01", 190.0), bar("2026-02-01", 200.0), bar("2026-03-01", 210.0),
          bar("2026-04-01", 220.0), bar("2026-05-01", 192.07), bar("2026-06-01", 301.15),
          bar("2026-07-01", 182.47), bar("2026-08-01", 176.0), bar("2026-09-01", 182.0)])
    w = [bar(f"2026-{i:02d}-01", 174.0 + i) for i in range(1, 9)]
    d = [bar(f"2026-08-{i:02d}", 180.0 + (i % 5)) for i in range(1, 29)]
    row_kl = engine.analyze({"symbol": "KLAC", "name": "科磊", "sector": "信息技术"},
                            m, w, d, CFG, TODAY)
    check("月K −39% 真实暴跌 → 不误判为坏数据（engine 只看日K）",
          row_kl["data_ok"], row_kl["data_note"])

    # ④ 跨周期口径不一致（月线复权 485 / 周线未复权 1850）
    gap = consistency_gap({"月": [bar("2026-09-01", 339.30)],
                           "周": [bar("2026-09-11", 1850.80)],
                           "日": [bar("2026-09-15", 339.30)]})
    check("月/周末值差 5.5 倍 → 识别为口径不一致", bool(gap), gap)

    # ⑤ 口径一致时不报警
    same = consistency_gap({"月": [bar("2026-09-01", 339.30)],
                            "周": [bar("2026-09-11", 341.00)],
                            "日": [bar("2026-09-15", 339.30)]})
    check("三周期末值基本一致 → 不报警", same == "", same)

    # ⑥ 【最关键】坏数据 + 完美信号 → 必须 SKIP，绝不下单
    row = make_row(data_ok=False, data_note="日K 1 处单日跳变")
    a = engine.decide(row, CFG, fresh_state())
    check("坏数据 + 完美新信号 → SKIP（不下单）",
          types_of(a) == {"SKIP"}, a)

    # ⑦ 坏数据 + 持仓已跌破止损 → 也不能卖（宁可不动，不可乱动）
    st = fresh_state()
    st["positions"] = {"TST": {"qty": 10, "entry_price": 100.0, "sector": "信息技术"}}
    a = engine.decide(make_row(data_ok=False, price=50.0), CFG, st)
    check("坏数据 + 持仓触发止损 → 仍不卖，等数据恢复",
          types_of(a) == {"SKIP"}, a)

    # ⑧ 数据正常时熔断不误伤
    a = engine.decide(make_row(data_ok=True), CFG, fresh_state())
    check("数据正常 → 熔断不误伤，正常 ARM", types_of(a) == {"ARM"}, a)


# ------------------------------------------------------------------ Part 5


def part5() -> None:
    """报表一致性：「入场闸门」列必须与 decide() 的判据（状态A ∧ 前置校验 ∧ 第一根红柱）一致。

    背景（真实踩坑）：window_open 只表示"信号月已过"，不表示"可以入场"。
    曾出现 LLY 前置校验不通过、表格却显示「🟢 已开」的误导。
    """
    print("\n── Part 5 · 报表「入场闸门」列一致性 ──")
    tmp = Path(tempfile.mkdtemp(prefix="holdle_gate_"))
    engine.LEDGER_DIR = tmp

    def render(**kw) -> str:
        return engine.write_ledger([make_row(**kw)], [], CFG, "dry", TODAY).read_text(encoding="utf-8")

    t = render(gate_ok=True)
    check("闸门全过 → 显示「🟢 已开」", "🟢 已开" in t)

    t = render(gate_ok=False, pre_ok=False, gate_reason="月K前置校验未通过")
    check("前置校验不过 → 显示「⛔ 闸门未过」", "⛔ 闸门未过" in t)
    check("前置校验不过 → 不得出现「🟢 已开」", "🟢 已开" not in t)

    t = render(gate_ok=False, state_a=False, gate_reason="当月状态A 不成立")
    check("状态A 不成立 → 不得出现「🟢 已开」", "🟢 已开" not in t)

    t = render(gate_ok=False, scenario=None, gate_reason="无有效「第一根红柱」信号")
    check("无信号 → 显示「—」", "| — |" in t)

    # 明细区的措辞也要一致
    t = render(gate_ok=False, pre_ok=False, gate_reason="月K前置校验未通过")
    check("明细区显示闸门未通过原因", "⛔ 未通过 —— 月K前置校验未通过" in t, )

    print(f"     （演练产物写入临时目录 {tmp}）")


# ------------------------------------------------------------------ Part 6


class _FakeMD:
    """假的行情源，让 report 的基准计算可以在离线状态下被测试。"""

    def __init__(self, bars):
        self._bars = bars

    def bars(self, symbol, tf):        # noqa: ARG002
        return list(self._bars)


def part6() -> None:
    """业绩报告的纯逻辑：基准收益率与最大回撤。

    背景（真实踩坑）：基准取"起始日或其后第一个交易日"的收盘价。
    期限刚开始那天还没有K线，原实现会回退到数据窗口最老的一根（约 400 天前），
    于是把整段窗口的涨幅当成"本期基准收益"（SPY 被算成 +18.75%）。
    """
    print("\n── Part 6 · 业绩报告基准与回撤 ──")
    import report as rep

    def bars_from(pairs):
        return [{"t": d, "c": c, "o": c, "h": c, "l": c, "v": 1.0} for d, c in pairs]

    real_md = rep.MarketData

    # ① 期限起始日还没有K线 → 基准必须为 0%，绝不能回退到最老那根
    old = bars_from([("2025-08-01", 400.0), ("2026-09-14", 600.0), ("2026-09-15", 600.0)])
    rep.MarketData = lambda feed: _FakeMD(old)      # noqa: ARG005
    b = rep.benchmark_return("SPY", "2026-09-16", "iex")
    check("起始日无K线 → 基准 = 0%（不回退到最老那根）",
          b is not None and abs(b["pct"]) < 1e-9, b)

    # ② 起始日之后有K线 → 用那根的收盘价做基准
    mid = bars_from([("2025-08-01", 400.0), ("2026-09-16", 500.0), ("2026-09-20", 550.0)])
    rep.MarketData = lambda feed: _FakeMD(mid)      # noqa: ARG005
    b = rep.benchmark_return("SPY", "2026-09-16", "iex")
    check("起始日有K线 → 基准 = 550/500-1 = +10%",
          b is not None and abs(b["pct"] - 10.0) < 1e-9, b)

    # ③ 起始日早于数据窗口 → 用窗口内第一根
    b = rep.benchmark_return("SPY", "2020-01-01", "iex")
    check("起始日早于数据窗口 → 用窗口首根（400 → 550 = +37.5%）",
          b is not None and abs(b["pct"] - 37.5) < 1e-9, b)

    rep.MarketData = real_md

    # ④ 最大回撤
    curve = [{"equity": 100.0}, {"equity": 120.0}, {"equity": 90.0}, {"equity": 110.0}]
    check("最大回撤 = (120-90)/120 = 25%",
          abs(rep.max_drawdown(curve) - 25.0) < 1e-9, rep.max_drawdown(curve))
    check("单调上升 → 回撤 0", rep.max_drawdown([{"equity": 1.0}, {"equity": 2.0}]) == 0.0)
    check("空曲线 → None", rep.max_drawdown([]) is None)

    # ⑤ 净值曲线里的 0 值（开户前）不能算进回撤
    with_zero = [{"equity": 0.0}, {"equity": 100.0}, {"equity": 80.0}]
    check("含开户前 0 值 → 回撤仍是 20%（不误算成 100%）",
          abs(rep.max_drawdown(with_zero) - 20.0) < 1e-9, rep.max_drawdown(with_zero))


# ------------------------------------------------------------------ Part 7


def part7() -> None:
    """入场机会已消耗（H 被突破过又回落）不得再买。

    背景（真实踩坑）：扫描器一次捞 184 只、信号回溯 4 个月，很容易捞到
    「几周前就已经突破过 H、现在又跌回来」的标的。若不加这道闸，系统会把
    **第二次突破**当成买点，而那是「错过初期不追」规则 明令禁止的「错过初期去追高」。

    真实案例 BAC：信号月 2026-06，H=62.66（07-27），08-12 收盘 64.48 已站上 H，
    到 09-18 又跌回 58.16。
    """
    print("\n── Part 7 · 入场机会已消耗（H 被突破过又回落）──")

    # ① 新信号 + 已被突破过 → SKIP，不能 ARM
    a = engine.decide(make_row(price=58.16, h_price=62.66, h_broken_date="2026-08-12"),
                      CFG, fresh_state())
    check("★ 已被突破过 → 不 ARM（BAC 型）", types_of(a) == {"SKIP"}, a)
    check("★ SKIP 理由点明突破日",
          any("2026-08-12" in x.get("reason", "") for x in a), a)

    # ② 新信号 + 从未突破过 → 正常 ARM（不能误伤）
    a = engine.decide(make_row(price=58.16, h_price=62.66, h_broken_date=None),
                      CFG, fresh_state())
    check("从未突破过 → 正常 ARM（不误伤）", types_of(a) == {"ARM"}, a)

    # ③ 等待中 + 已被突破过 + 现价已回落 → CANCEL
    st = fresh_state()
    st["pending"] = {"TST": {"signal_month": "2026-06", "h_price": 62.66,
                             "deadline": "2026-12-01"}}
    a = engine.decide(make_row(price=58.16, h_price=62.66, h_broken_date="2026-08-12"),
                      CFG, st)
    check("★ 等待中 + 机会已消耗 → CANCEL", types_of(a) == {"CANCEL"}, a)

    # ④ 等待中 + 突破过 + 现价仍在 H 上方 → 仍要 BUY（别把正常突破当成已消耗）
    a = engine.decide(make_row(price=70.0, h_price=62.66, h_broken_date="2026-08-12"),
                      CFG, st)
    check("现价仍在 H 上方 → 照常 BUY（不误伤正常突破）",
          types_of(a) == {"BUY"}, a)

    # ⑤ 等待中 + 未突破过 + 未达 H → WAIT
    a = engine.decide(make_row(price=58.16, h_price=62.66, h_broken_date=None),
                      CFG, st)
    check("未突破过且未达 H → WAIT", types_of(a) == {"WAIT"}, a)

    # ⑤之二 新信号 + 已被突破过 + 现价仍在 H 上方 → 仍要 SKIP，但理由不能写错
    a = engine.decide(make_row(price=70.0, h_price=62.66, h_broken_date="2026-08-05"),
                      CFG, fresh_state())
    check("★ 突破过但现价仍在 H 上方 → 仍 SKIP（分不清已走多远）",
          types_of(a) == {"SKIP"}, a)
    check("★ 理由必须写「仍在其上」，不能写成「已回落其下」（CVX 踩过）",
          any("仍在其上" in x.get("reason", "") for x in a), a)
    a = engine.decide(make_row(price=58.16, h_price=62.66, h_broken_date="2026-08-05"),
                      CFG, fresh_state())
    check("★ 现价已回落 → 理由写「已回落其下」",
          any("已回落其下" in x.get("reason", "") for x in a), a)

    # ⑥ 坏数据优先级仍高于本闸（先体检，再谈机会）
    a = engine.decide(make_row(price=58.16, h_price=62.66, h_broken_date="2026-08-12",
                               data_ok=False), CFG, fresh_state())
    check("数据体检不过 → 仍优先报 SKIP 数据问题", types_of(a) == {"SKIP"}, a)
    check("数据问题的理由优先于机会消耗",
          any("数据" in x.get("reason", "") or "跳变" in x.get("reason", "")
              for x in a), a)


# ------------------------------------------------------------------ Part 8


def part8() -> None:
    """等待入场时，买入判据必须用**当场算出的**参考价 H，不能用 state 里的旧快照。

    背景（2026-09-19 踩到）：state 里的 h_price 是 ARM 那天写进去的。只要 H 的算法
    或数据口径有变化，它就变成旧值；而 decide() 跑在 execute() 之前，本轮的新 H
    要等这轮结束才写回 state。于是会出现「买入判据拿旧 H」的错位：
    SCHW 旧 H=109.56 / 新 H=110.69，若股价落在 109.56~110.69 之间，
    系统就会**在没真正突破前高时买入**。不报错，只会悄悄多买一次。
    """
    print("\n── Part 8 · 参考价必须取当场算出的值（不能用 state 旧快照）──")

    def pend_state(h):
        st = fresh_state()
        st["pending"] = {"TST": {"signal_month": "2026-08", "h_price": h,
                                 "h_date": "2026-08-10", "deadline": "2026-12-01"}}
        return st

    # ① 旧快照 100 / 本轮新算 110 / 现价 105 → 没突破新 H，必须 WAIT
    a = engine.decide(make_row(price=105.0, h_price=110.0), CFG, pend_state(100.0))
    check("★ 现价高于旧 H 但低于新 H → WAIT（不能按旧 H 买）",
          types_of(a) == {"WAIT"}, a)
    check("★ WAIT 理由写的是新 H 110.00",
          any("110.00" in x.get("reason", "") for x in a), a)

    # ② 旧快照 110 / 本轮新算 100 / 现价 105 → 已突破新 H，必须 BUY
    a = engine.decide(make_row(price=105.0, h_price=100.0), CFG, pend_state(110.0))
    check("★ 现价低于旧 H 但已高于新 H → BUY（不能按旧 H 漏掉）",
          types_of(a) == {"BUY"}, a)
    check("★ BUY 理由写的是新 H 100.00",
          any("100.00" in x.get("reason", "") for x in a), a)

    # ③ 本轮 H 尚未形成 → 退回 state 里的值兜底（不能把等待中的标的弄丢）
    a = engine.decide(make_row(price=105.0, h_price=None), CFG, pend_state(100.0))
    check("本轮 H 未形成 → 用 state 里的旧值兜底并正常 BUY",
          types_of(a) == {"BUY"}, a)

    # ④ 失效日也优先用本轮算出的
    st = pend_state(100.0)
    st["pending"]["TST"]["deadline"] = "2026-12-01"
    a = engine.decide(make_row(price=90.0, h_price=100.0, deadline="2026-09-01"),
                      CFG, st)
    check("★ 本轮算出的失效日已过 → CANCEL（不因为 state 里没过就放过）",
          types_of(a) == {"CANCEL"}, a)


if __name__ == "__main__":
    part1()
    part2()
    part3()
    part4()
    part5()
    part6()
    part7()
    part8()
    print(f"\n演练结果：{OK} 通过 / {FAIL} 失败")
    raise SystemExit(1 if FAIL else 0)
