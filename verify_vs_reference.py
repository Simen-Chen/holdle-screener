"""
对照校验：把真实历史月线回灌 rules.py，看算出来的 MACD 与「第一根红柱」信号
是否与回测报告 / 观察池记录一致。

数据源：westock 月线（usNVDA，2016-01 → 2021-06，×40 口径的收盘价序列）
参照值：westock `technical --period month` 的 DIF / DEA / 柱（÷40 口径）

⚠️ 这是「引擎口径自检」，不是策略回测。跑法：
    python verify_vs_reference.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rules   # noqa: E402

# ---- 真实月线收盘（westock kline usNVDA，2016-01 → 2021-06，×40 口径） ----
MONTHS = (
    [f"2016-{m:02d}" for m in range(1, 13)] +
    [f"2017-{m:02d}" for m in range(1, 13)] +
    [f"2018-{m:02d}" for m in range(1, 13)] +
    [f"2019-{m:02d}" for m in range(1, 13)] +
    [f"2020-{m:02d}" for m in range(1, 13)] +
    [f"2021-{m:02d}" for m in range(1, 7)]
)
CLOSES = [
    29.29, 31.36, 35.63, 35.53, 46.72, 47.01, 57.10, 61.34, 68.52, 71.16, 92.20, 106.74,
    109.18, 101.48, 108.93, 104.30, 144.35, 144.56, 162.51, 169.44, 178.77, 206.81, 200.71, 193.50,
    245.80, 242.00, 231.59, 224.90, 252.19, 236.90, 244.86, 280.68, 281.02, 210.83, 163.43, 133.50,
    143.75, 154.26, 179.56, 181.00, 135.46, 164.23, 168.72, 167.51, 174.07, 201.02, 216.74, 235.30,
    236.43, 270.07, 263.60, 292.28, 355.02, 379.91, 424.59, 534.98, 541.22, 501.36, 536.06, 522.20,
    519.59, 548.58, 533.93, 600.38, 649.78, 800.10,
]

# ---- 参照值（westock technical，÷40 口径） ----
REFERENCE = {
    "2016-01": (0.086061, 0.062271, 0.047581),
    "2016-09": (0.258849, 0.165526, 0.186645),
    "2017-05": (0.621833, 0.458234, 0.327198),
    "2018-08": (1.202600, 1.116800, 0.171700),
    "2018-10": (1.091100, 1.129000, -0.075800),
    "2019-12": (0.266200, 0.226800, 0.078800),
    "2020-01": (0.334628, 0.248359, 0.172537),
    "2020-09": (1.948178, 1.119875, 1.656605),
    "2021-05": (2.564622, 2.171824, 0.785595),
    "2021-06": (2.943887, 2.326237, 1.235300),
}

# ---- 信号期望（来自回测报告 / 观察池记录） ----
EXPECTED_SIGNALS = [
    # (截止月, 期望场景 or None, 说明)
    ("2016-09", None, "起点已在状态A 中段，无新鲜第一根红柱 → 不入场"),
    ("2018-08", rules.SCENARIO2, "高位伪信号（2 个月后死叉，随后 −57%）"),
    ("2019-12", rules.SCENARIO1, "由绿转红 → 2020-02 入场"),
    ("2021-05", rules.SCENARIO2, "柱 +0.791 vs 上月 +0.706 = +12.1%，≥×1.10 → 触发"),
    ("2021-04", None, "柱 +0.706 vs 上月 +0.702 = +0.5%，噪音级 → 不触发"),
]

TOL = 0.05          # MACD 允许 5% 相对偏差
WARMUP = 36         # 前 36 根柱为 EMA 预热期，不参与数值对照（见第五节）
CONFIG_LOOKBACK = 60   # config.json 里 monthly_lookback_months 的值
OK = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


def main() -> None:
    scale = 40.0    # westock kline 是 technical 的 40 倍
    dif, dea, hist = rules.macd(CLOSES)
    idx = {m: i for i, m in enumerate(MONTHS)}

    print("── 一、MACD 数值对照（引擎 vs westock 参照值）──")
    worst = 0.0
    worst_at = ""
    for m, (rd, re_, rh) in REFERENCE.items():
        i = idx[m]
        d, e, h = dif[i] / scale, dea[i] / scale, hist[i] / scale
        dev = max(abs(d - rd) / max(abs(rd), 1e-9),
                  abs(e - re_) / max(abs(re_), 1e-9),
                  abs(h - rh) / max(abs(rh), 1e-9))
        if i < WARMUP:
            flag = "🌱"
            note = "（预热期，不判定）"
        else:
            flag = "✅" if dev <= TOL else "⚠️"
            note = ""
            if dev > worst:
                worst, worst_at = dev, m
        print(f"  {flag} {m}  引擎 DIF {d:+.4f} / DEA {e:+.4f} / 柱 {h:+.4f}"
              f"   参照 {rd:+.4f} / {re_:+.4f} / {rh:+.4f}   偏差 {dev*100:.2f}%{note}")

    check(f"预热期外全部检查点偏差 ≤ {TOL*100:.0f}%（最大 {worst*100:.2f}% @ {worst_at}）",
          worst <= TOL)
    print(f"  注：前 {WARMUP} 根柱的 EMA 尚未收敛，参照值本身来自更长历史，")
    print(f"      两者不可比；实盘只判最近几根，故不影响信号。")

    print("\n── 二、信号判定对照（引擎 vs 回测报告结论）──")
    for m, expect, why in EXPECTED_SIGNALS:
        i = idx[m]
        sub = hist[:i + 1]
        s, _, detail = rules.recent_first_red_bar(sub, 1.10, 2, 4)
        ok = (s == expect)
        check(f"{m} → {expect or '无信号'}", ok, f"实际 {s}｜{detail}")
        print(f"      （{why}）")

    print("\n── 三、状态A 判定对照 ──")
    for m, want in [("2016-09", True), ("2018-10", False), ("2019-12", True),
                    ("2020-09", True), ("2021-03", True)]:
        i = idx[m]
        got = rules.is_state_a(dif, dea, hist, i)
        check(f"{m} 状态A = {want}", got == want,
              f"DIF {dif[i]/scale:+.3f} DEA {dea[i]/scale:+.3f} 柱 {hist[i]/scale:+.3f}")

    print("\n── 四、柱体形态对照（NVDA 2021 收缩→放大 真实形态）──")
    i3, i4, i5 = idx["2021-03"], idx["2021-04"], idx["2021-05"]
    h3, h4, h5 = hist[i3] / scale, hist[i4] / scale, hist[i5] / scale
    check("2021-03 是收缩谷底（低于上月）",
          rules.is_contracting(hist[:i3 + 1]),
          f"2021-02 {hist[i3-1]/scale:+.4f} → 2021-03 {h3:+.4f}")
    check("2021-04 谷底后噪音级微升（未达触发线）",
          h4 > h3 and h4 <= h3 * 1.10,
          f"{h3:+.4f} → {h4:+.4f}（{(h4/h3-1)*100:+.1f}%）")
    check("2021-05 柱体明显放大（≥ 上月×1.10）",
          h5 > h4 * 1.10,
          f"{h4:+.4f} → {h5:+.4f}（{(h5/h4-1)*100:+.1f}%）")
    c = rules._contraction_run(hist, i5, 1.10)
    check(f"收缩段长度 {c} ≥ 2（噪音微升被正确并入谷底）", c >= 2)

    print("\n── 五、EMA 预热收敛检验（决定 monthly_lookback_months）──")
    rd, re_, rh = REFERENCE["2021-06"]
    print(f"  基准 = westock 2021-06 参照值 DIF {rd:+.4f} / DEA {re_:+.4f} / 柱 {rh:+.4f}")
    tail_dev = {}
    for n in (24, 36, 48, 60, 66):
        d, e, h = rules.macd(CLOSES[-n:])
        dd, ee, hh = d[-1] / scale, e[-1] / scale, h[-1] / scale
        dev = max(abs(dd - rd) / abs(rd), abs(ee - re_) / abs(re_), abs(hh - rh) / abs(rh))
        tail_dev[n] = dev
        flag = "✅" if dev <= 0.02 else "⚠️"
        print(f"  {flag} 尾部 {n:>2} 根 → DIF {dd:+.4f} / DEA {ee:+.4f} / 柱 {hh:+.4f}"
              f"   最大偏差 {dev*100:5.2f}%")
    check(f"config 取值 {CONFIG_LOOKBACK} 根月线时，末柱偏差 ≤ 2%",
          tail_dev[CONFIG_LOOKBACK] <= 0.02,
          f"实际 {tail_dev[CONFIG_LOOKBACK]*100:.2f}%")

    print(f"\n校验结果：{OK} 通过 / {FAIL} 失败")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
