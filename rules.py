"""
HOLDLE 体系 · 规则引擎（纯函数，零外部依赖）

规则分为「择时」与「风控」两组，见 docs/methodology.md。
所有参数从 config.json 的 rules 段读入，本文件不写死任何阈值。

本文件可以单独跑：python rules.py 会执行内置自检。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------- 指标

def ema(values: list[float], period: int) -> list[float]:
    """指数移动平均。首值用第一个样本做种子。"""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out: list[float] = []
    prev = values[0]
    for v in values:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (DIF, DEA, 柱)。

    柱 = 2 × (DIF − DEA) —— 通达信/自选股口径，与回测报告的数值口径一致。
    """
    if not closes:
        return [], [], []
    ef, es = ema(closes, fast), ema(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, signal)
    hist = [2.0 * (a - b) for a, b in zip(dif, dea)]
    return dif, dea, hist


# ---------------------------------------------------------------- 择时·状态A

def is_state_a(dif: list[float], dea: list[float], hist: list[float], i: int = -1) -> bool:
    """状态A = 月线 DIF > 0 且 DEA > 0 且 柱 > 0（三项同时）。"""
    if not dif or len(dif) < abs(i):
        return False
    return dif[i] > 0 and dea[i] > 0 and hist[i] > 0


def precheck(highs: list[float], lows: list[float], lookback: int = 5,
             drop_last: bool = False) -> tuple[bool, str]:
    """前置校验：月K低点逐月抬高 + 高点逐月抬高。

    drop_last=True 时忽略最后一根（当月未走完）。
    """
    h = list(highs[-lookback:])
    l = list(lows[-lookback:])
    if drop_last:
        h, l = h[:-1], l[:-1]
    if len(h) < 3:
        return False, f"样本不足（仅 {len(h)} 个完整月）"
    ok_h = all(h[i] > h[i - 1] for i in range(1, len(h)))
    ok_l = all(l[i] > l[i - 1] for i in range(1, len(l)))
    fmt = lambda xs: " → ".join(f"{x:.2f}" for x in xs)
    detail = f"高点[{'升' if ok_h else '未升'}] {fmt(h)} ｜ 低点[{'升' if ok_l else '未升'}] {fmt(l)}"
    return (ok_h and ok_l), detail


# ---------------------------------------------------------------- 择时·第一根红柱与重入

SCENARIO1 = "场景一·由绿转红"
SCENARIO2 = "场景二·由矮变高"


def _contraction_run(hist: list[float], i: int, multiplier: float = 1.10) -> int:
    """从 i-1 往前统计「连续收缩月数」。

    容忍谷底之后的噪音级微升：若 hist[i-1] 高于 hist[i-2]，但这点升幅本身
    还没够到 multiplier 触发线（< +10%），说明它只是谷底附近的噪音，不是一次
    「由矮变高」，把它并入谷底后继续往前数收缩。

    为什么必须容忍 —— 真实案例（NVDA 月线柱）：
        2020-09 +1.669 → 10 -7.3% → 11 -4.6% → 12 -13.1% → 2021-01 -17.7%
        → 02 -13.0% → 03 +0.702（谷底）→ 04 +0.706（+0.5% 噪音）→ 05 +0.791（+12.1%）
    若严格要求「上月仍在下行」，2021-05 这根真信号会被整个漏掉。
    """
    j = i - 1
    if j >= 1 and hist[j] >= hist[j - 1] and hist[j] <= hist[j - 1] * multiplier:
        j -= 1
    c = 0
    while j > 0 and hist[j] < hist[j - 1]:
        c += 1
        j -= 1
    return c


def _first_red_bar_at(hist: list[float], i: int, multiplier: float,
                      min_contraction: int) -> tuple[Optional[str], str]:
    """判定第 i 根柱是否为「第一根红柱」。"""
    if i < 1:
        return None, ""

    # 场景一：由绿转红
    if hist[i] > 0 and hist[i - 1] < 0:
        return SCENARIO1, f"{hist[i-1]:+.4f} → {hist[i]:+.4f}（由绿转红）"

    # 场景二：由矮变高（连续多月递减后，本月 > 上月 × multiplier）
    if hist[i] > 0 and hist[i - 1] > 0 and hist[i] > hist[i - 1] * multiplier:
        c = _contraction_run(hist, i, multiplier)
        if c >= min_contraction:
            pct = (hist[i] / hist[i - 1] - 1) * 100
            return SCENARIO2, (
                f"连续收缩 {c} 个月后 {hist[i-1]:+.4f} → {hist[i]:+.4f}"
                f"（+{pct:.1f}%，≥ 上月×{multiplier}）"
            )

    return None, ""


def first_red_bar(hist: list[float], multiplier: float = 1.10,
                  min_contraction: int = 2) -> tuple[Optional[str], Optional[int], str]:
    """判定【最新一根】柱体是否为第一根红柱。"""
    n = len(hist)
    if n < 3:
        return None, None, "柱体序列不足 3 期"
    i = n - 1
    s, d = _first_red_bar_at(hist, i, multiplier, min_contraction)
    if s:
        return s, i, d
    return None, None, f"本月柱 {hist[i]:+.4f}，上月 {hist[i-1]:+.4f}，未构成第一根红柱"


def recent_first_red_bar(hist: list[float], multiplier: float = 1.10,
                         min_contraction: int = 2,
                         lookback: int = 4) -> tuple[Optional[str], Optional[int], str]:
    """在最近 lookback 根柱体里找【最近一次】第一根红柱。

    为什么需要它：本脚本是每日扫描的，而月线指标每月才更新一次。
    如果只判"最新一根"，一旦月份翻页，上个月刚出现的信号就会被漏掉。
    """
    n = len(hist)
    if n < 3:
        return None, None, "柱体序列不足 3 期"
    for i in range(n - 1, max(0, n - 1 - lookback) - 1, -1):
        if i < 1:
            break
        s, d = _first_red_bar_at(hist, i, multiplier, min_contraction)
        if s:
            return s, i, d
    return None, None, f"最近 {lookback} 个月内无第一根红柱"


def is_contracting(hist: list[float]) -> bool:
    """本月柱 < 上月柱（收缩中）。用于提案 R-2 的「月柱不再收缩」闸。"""
    return len(hist) >= 2 and hist[-1] < hist[-2]


# ---------------------------------------------------------------- 三级止损

@dataclass
class StopInfo:
    line: float
    stage: int
    label: str


def stop_line(entry_price: float, current_price: float, cfg: dict) -> StopInfo:
    """三级止损 三级止损。"""
    if current_price >= entry_price * cfg["trail_trigger_3"]:
        return StopInfo(entry_price * cfg["stop_loss_3"], 3, "三级·保本")
    if current_price >= entry_price * cfg["trail_trigger_2"]:
        return StopInfo(entry_price * cfg["stop_loss_2"], 2, "二级·−10%")
    return StopInfo(entry_price * cfg["stop_loss_1"], 1, "一级·−20%")


# ---------------------------------------------------------------- 周K有效低点规则

def weekly_effective_low(highs: list[float], lows: list[float], hist: list[float],
                         window: int = 3) -> tuple[Optional[float], list[tuple[str, float]]]:
    """周K有效低点规则 周K有效低点（近似实现）。

    ① 找摆动低点 L
    ② 回撤期间周K MACD 出现绿柱
    ③ 随后股价创新高
    → L 成为出场参考价；后续更高且创新高的有效低点逐级取代。

    返回 (最新参考价 or None, [(日期标签, 低点), ...]) —— 标签由调用方在外部对齐，
    这里只返回下标与数值，日期由 engine 补。
    """
    n = len(highs)
    if n < window * 2 + 4:
        return None, []

    swing_lows = [i for i in range(window, n - window)
                  if lows[i] == min(lows[i - window:i + window + 1])]
    if not swing_lows:
        return None, []

    valid: list[tuple[int, float]] = []
    for i in swing_lows:
        # 找它前面最近的一个摆动高点
        lo = max(0, i - window * 4)
        cand = [j for j in range(lo + window, i - 1)
                if highs[j] == max(highs[max(0, j - window):j + window + 1])]
        if not cand:
            continue
        j = cand[-1]
        # ② 回撤期间必须有周K绿柱
        seg = hist[j:i + 1]
        if not seg or min(seg) >= 0:
            continue
        # ③ 随后必须创新高
        after = highs[i + 1:]
        if not after or max(after) <= highs[j]:
            continue
        # 还得真的回撤过（低点低于前高）
        if lows[i] >= highs[j]:
            continue
        valid.append((i, lows[i]))

    if not valid:
        return None, []

    # 安全线逐级上移：只保留比前一个更高的
    seq: list[tuple[int, float]] = []
    for i, lv in valid:
        if not seq or lv > seq[-1][1]:
            seq.append((i, lv))

    return seq[-1][1], seq


# ---------------------------------------------------------------- 择时·参考价 H

def find_reference_high(daily_highs: list[float], daily_lows: list[float],
                        daily_hist: list[float], start_idx: int,
                        window: int = 5) -> tuple[Optional[int], Optional[float], str]:
    """择时·参考价 H：在 start_idx 之后找「冲高 → 回撤且日K出绿柱」的第一个高点 H。

    返回 (H 下标, H 价格, 说明)

    🔴 局部高点的比较窗口以 **start_idx（入场窗口左边界）截断**，
       不要求 i 左侧凑满 window 根K线。

    为什么必须这样（2026-09-19 修的 bug）：
    原实现从 `range(start_idx + window, n - 1)` 起找，等于规定
    **「入场窗口内最早 5 根K线永远不能当参考价」**。可「冲高」恰恰最可能
    就发生在 T+1 月刚开头那几天 —— 那本来就是资金刚进场的位置。

    真实案例 V：入场窗口 2026-09-01 起，窗口内最高点 **382.06 落在第 3 根
    （09-03）**，被永久跳过；后面又没有更高的局部高点，于是 V 一直显示
    「等待日K形成冲高+回撤出绿柱」，**一路静默空转到 10-31 失效**。
    不报错、也不干活 —— 这类问题比报错难发现得多。

    旧口径的动机是"保证 i 左侧也有 window 根K线可判局部高点"，但它把
    **入场窗口的边界**错当成了**数据的边界**。正确做法是用 `max(start_idx, i-window)`
    把比较窗口在左边界处截断即可。
    """
    n = len(daily_highs)
    if n - start_idx < window * 2 + 3:
        return None, None, "信号月之后日K样本不足"

    for i in range(start_idx, n - 1):
        lo = max(start_idx, i - window)
        hi = min(n, i + window + 1)
        if daily_highs[i] != max(daily_highs[lo:hi]):
            continue
        after_lows = daily_lows[i + 1:]
        after_hist = daily_hist[i + 1:]
        if not after_lows or not after_hist:
            continue
        if min(after_lows) >= daily_highs[i]:
            continue                     # 没真回撤
        if min(after_hist) >= 0:
            continue                     # 回撤期间没有日K绿柱 → 参考价不成立
        return i, daily_highs[i], f"高点 H={daily_highs[i]:.2f}，回撤已出日K绿柱"

    return None, None, "尚未形成「冲高 + 回撤出绿柱」的组合"


# ---------------------------------------------------------------- 自检

def _selftest() -> None:
    ok = 0
    fail = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✅ {name}")
        else:
            fail += 1
            print(f"  ❌ {name} {extra}")

    print("── MACD 口径 ──")
    closes = [float(i) for i in range(1, 80)]
    dif, dea, hist = macd(closes)
    check("柱 = 2×(DIF−DEA)", all(abs(h - 2 * (d - e)) < 1e-9
                                  for h, d, e in zip(hist, dif, dea)))
    check("上升序列 DIF > DEA", dif[-1] > dea[-1])
    check("上升序列柱 > 0", hist[-1] > 0)

    print("── 状态A ──")
    check("三线全正 → 状态A", is_state_a([1.0], [0.5], [1.0]))
    check("柱为负 → 非状态A", not is_state_a([1.0], [0.5], [-1.0]))
    check("DIF 为负 → 非状态A", not is_state_a([-1.0], [0.5], [1.0]))

    print("── 前置校验 ──")
    hi = [100, 110, 120, 130, 140]
    lo = [90, 95, 100, 105, 110]
    check("高低点逐月抬高 → 通过", precheck(hi, lo)[0])
    check("高点走平 → 不通过", not precheck([100, 110, 110, 120, 130], lo)[0])
    check("忽略当月：末月回落仍通过", precheck([100, 110, 120, 130, 125],
                                              [90, 95, 100, 105, 102],
                                              drop_last=True)[0])

    print("── 第一根红柱 ──")
    s, _, _ = first_red_bar([-0.5, -0.3, -0.1, 0.2])
    check("由绿转红 → 场景一", s == SCENARIO1)
    s, _, _ = first_red_bar([1.0, 0.8, 0.6, 0.5, 0.7])
    check("收缩 3 月后 +40% → 场景二", s == SCENARIO2)
    s, _, _ = first_red_bar([1.0, 0.8, 0.6, 0.5, 0.505])
    check("收缩后仅 +1% → 不触发（噪音）", s is None)
    s, _, _ = first_red_bar([0.5, 0.6, 0.7, 0.8])
    check("单调放大 → 不触发", s is None)
    # 谷底噪音微升（复现 NVDA 2021-03→04→05 真实形态）
    s, _, _ = first_red_bar([1.0, 0.8, 0.6, 0.5, 0.502, 0.62])
    check("谷底 +0.4% 噪音后 +23% → 场景二（容忍微升）", s == SCENARIO2)
    s, _, _ = first_red_bar([1.0, 0.8, 0.6, 0.5, 0.58, 0.64])
    check("微升本身已达触发线 → 信号在前一月，本月不再重复", s is None)
    s, _, _ = first_red_bar([1.0, 0.5, 0.6])
    check("仅收缩 1 个月 → 不触发（不足 min_contraction）", s is None)

    print("── 三级止损 ──")
    cfg = {"stop_loss_1": 0.8, "stop_loss_2": 0.9, "stop_loss_3": 1.0,
           "trail_trigger_2": 1.2, "trail_trigger_3": 1.3}
    check("刚买入 → P×0.80", abs(stop_line(100, 100, cfg).line - 80) < 1e-9)
    check("涨 25% → P×0.90", abs(stop_line(100, 125, cfg).line - 90) < 1e-9)
    check("涨 35% → 保本 P", abs(stop_line(100, 135, cfg).line - 100) < 1e-9)

    print("── 参考价 H（入场窗口边界）──")
    # 复现 V 2026-09 的真实形态：窗口内最高点落在**第 3 根**。
    # 旧口径从 start_idx+window（第 5 根）起找，会永久跳过它 → H 恒为 None → 静默空转。
    v_high = [380.75, 380.17, 382.06, 377.27, 373.38, 369.45, 367.99,
              371.74, 377.12, 376.34, 375.27, 372.45, 370.71]
    v_low = [372.27, 375.00, 376.94, 373.66, 367.53, 365.99, 365.54,
             368.03, 371.67, 372.89, 368.20, 368.43, 367.02]
    v_hist = [0.266, -0.037, -0.297, -1.026, -2.309, -3.252, -3.808,
              -3.623, -2.770, -2.143, -2.266, -2.414, -2.724]
    i_v, p_v, _ = find_reference_high(v_high, v_low, v_hist, 0)
    check("★ 回归：窗口首段的最高点必须能被选为 H（V 型，旧实现返回 None）",
          i_v is not None, f"idx={i_v}")
    check("★ 选中的是窗口内真实最高点 382.06", p_v == 382.06, f"{p_v}")
    check("★ 落在第 3 根（下标 2），即旧口径跳过的那一根", i_v == 2, f"{i_v}")

    # 首根不是局部最高 → 不能因为"左边界截断"就被当成高点
    h2 = [100, 105, 103, 101, 99, 98, 97, 96, 95, 94, 93, 92, 91]
    l2 = [90] * 13
    hist2 = [0.5, 0.4, -0.1, -0.2, -0.3, -0.4, -0.5,
             -0.6, -0.7, -0.8, -0.9, -1.0, -1.1]
    i2, p2, _ = find_reference_high(h2, l2, hist2, 0)
    check("★ 边界截断不误伤：首根低于次根 → 选次根 105", p2 == 105.0, f"{p2}")

    # 回撤没出绿柱 → 不成立（原有语义不能丢）
    i3, p3, _ = find_reference_high(v_high, v_low, [1.0] * 13, 0)
    check("回撤期间无绿柱 → 参考价不成立", p3 is None, f"{p3}")

    # 样本不足 → 不硬算
    i4, p4, note4 = find_reference_high(v_high[:8], v_low[:8], v_hist[:8], 0)
    check("样本不足 → 返回 None 并说明", p4 is None and "样本不足" in note4, note4)

    print("── 周K有效低点 ──")
    # 构造：上冲 → 回撤（周K绿柱）→ 创新高 → 再回撤（周K绿柱）→ 再创新高
    highs = [10, 12, 14, 13, 11, 12, 13, 15, 17, 16, 14, 15, 16, 18, 20, 19, 21]
    lows = [9, 11, 13, 12, 10, 11, 12, 14, 16, 15, 13, 14, 15, 17, 19, 18, 20]
    hist = [0.5, 0.6, 0.7, 0.3, -0.2, 0.1, 0.4, 0.6, 0.8, 0.2, -0.3,
            0.1, 0.3, 0.5, 0.7, 0.2, 0.4]
    ref, seq = weekly_effective_low(highs, lows, hist, window=2)
    check("识别出有效低点", ref is not None, f"ref={ref}")
    check("参考价逐级上移", len(seq) >= 2 and all(
        seq[i][1] > seq[i - 1][1] for i in range(1, len(seq))), f"seq={seq}")

    print(f"\n自检结果：{ok} 通过 / {fail} 失败")
    if fail:
        raise SystemExit(1)


if __name__ == "__main__":
    _selftest()
