"""
主引擎：扫描观察池 → 按模块C/D 出决策 → （可选）在 Alpaca 模拟盘下单 → 记账。

用法：
    python run.py scan       只扫描，输出决策与计划（不下单）
    python run.py run        按 config.mode 执行（默认 dry）
    python run.py report     只看最新持仓状态

所有判定都走 rules.py，本文件只负责流程与记账。
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import rules
from alpaca_io import MarketData, PaperBroker, consistency_gap, split_cliffs

BASE = Path(__file__).parent
STATE_DIR = BASE / "state"
LEDGER_DIR = BASE / "ledger"
STATE_FILE = STATE_DIR / "portfolio.json"


# ------------------------------------------------------------------ 状态

def empty_state() -> dict:
    return {"positions": {}, "pending": {}, "cycles": {}, "history": []}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return empty_state()
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return empty_state()


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ------------------------------------------------------------------ 单个标的分析

def _month_key(date_str: str) -> str:
    return date_str[:7]


def monthly_signal(item: dict, monthly: list[dict], cfg: dict, today: str) -> dict:
    """只用月线就能算出来的那部分判定（模块C 3.1 / 3.2 / 3.4）。

    为什么单独抽出来：`screen.py` 的第一级粗筛要扫 180+ 只票，只取月线；
    它必须和 `analyze()` 走**同一份代码**。两处各写一遍迟早会漂移，而且这种漂移
    极难发现 —— 粗筛说通过、全量分析说不通过，两边都不报错，你只会觉得"怎么没信号"。
    """
    R = cfg["rules"]
    sym = item["symbol"]

    m_close = [b["c"] for b in monthly]
    m_high = [b["h"] for b in monthly]
    m_low = [b["l"] for b in monthly]
    dif, dea, hist = rules.macd(m_close, R["macd_fast"], R["macd_slow"], R["macd_signal"])

    drop_last = bool(R["precheck_ignore_current_month"]) and \
        monthly and _month_key(monthly[-1]["t"]) == _month_key(today)
    pre_ok, pre_detail = rules.precheck(
        m_high, m_low, R["precheck_lookback_months"], drop_last)

    state_a = rules.is_state_a(dif, dea, hist)
    scenario, sig_idx, scen_detail = rules.recent_first_red_bar(
        hist, R["scenario2_multiplier"], R["scenario2_min_contraction_months"],
        R.get("signal_lookback_months", 4))
    signal_state_a = (rules.is_state_a(dif, dea, hist, sig_idx)
                      if sig_idx is not None else False)
    if scenario and not signal_state_a:
        scenario, sig_idx, scen_detail = None, None, "信号月状态A 不成立，忽略该信号"
    contracting = rules.is_contracting(hist)

    signal_month = _month_key(monthly[sig_idx]["t"]) if sig_idx is not None else None
    # window_open 只表示"信号月已过、进入次月入场期"，**不等于可以入场**。
    window_open = bool(scenario and signal_month and signal_month < _month_key(today))

    # 入场闸门 = 状态A ∧ 前置校验 ∧ 第一根红柱（与 decide() ③ 的判据保持一致）。
    # 报表必须用 gate_ok，否则会出现"前置校验不过却显示窗口已开"的误导。
    gate_ok = bool(state_a and pre_ok and scenario)
    if not scenario:
        gate_reason = "无有效「第一根红柱」信号"
    elif not state_a:
        gate_reason = "当月状态A 不成立"
    elif not pre_ok:
        gate_reason = "月K前置校验未通过"
    else:
        gate_reason = ""

    return {
        "symbol": sym, "name": item.get("name", sym),
        "sector": item.get("sector", "—"), "today": today,
        "dif": dif[-1] if dif else 0.0,
        "dea": dea[-1] if dea else 0.0,
        "hist": hist[-1] if hist else 0.0,
        "hist_prev": hist[-2] if len(hist) >= 2 else 0.0,
        "state_a": state_a, "signal_state_a": signal_state_a,
        "pre_ok": pre_ok, "pre_detail": pre_detail,
        "scenario": scenario, "scenario_detail": scen_detail,
        "signal_month": signal_month, "window_open": window_open,
        "gate_ok": gate_ok, "gate_reason": gate_reason,
        "contracting": contracting,
        "sig_idx": sig_idx,
    }


def analyze(item: dict, monthly: list[dict], weekly: list[dict],
            daily: list[dict], cfg: dict, today: str) -> dict:
    R = cfg["rules"]
    sym = item["symbol"]

    sig = monthly_signal(item, monthly, cfg, today)
    m_close = [b["c"] for b in monthly]
    scenario, sig_idx = sig["scenario"], sig["sig_idx"]
    signal_month = sig["signal_month"]
    window_open = sig["window_open"]

    # 周K有效低点
    w_close = [b["c"] for b in weekly]
    _, _, w_hist = rules.macd(w_close, R["macd_fast"], R["macd_slow"], R["macd_signal"])
    w_ref, w_seq = rules.weekly_effective_low(
        [b["h"] for b in weekly], [b["l"] for b in weekly],
        w_hist, R["swing_window_weeks"])
    w_seq_labelled = [
        (weekly[i]["t"] if i < len(weekly) else "?", v) for i, v in w_seq
    ]

    # 日K参考高点 H（信号月的次月起算）
    h_idx = h_price = None
    h_detail = "尚未进入入场窗口"
    if scenario and signal_month:
        if window_open:
            start_idx = next(
                (i for i, b in enumerate(daily) if _month_key(b["t"]) > signal_month), None)
            if start_idx is not None:
                d_close = [b["c"] for b in daily]
                _, _, d_hist = rules.macd(d_close, R["macd_fast"], R["macd_slow"],
                                          R["macd_signal"])
                h_idx, h_price, h_detail = rules.find_reference_high(
                    [b["h"] for b in daily], [b["l"] for b in daily], d_hist, start_idx)
            else:
                h_detail = "次月日K数据尚未产生"
        else:
            h_detail = f"T 月 = {signal_month}（当月），次月起切日K"

    # H 之后是否已经被「收盘价」站上过（只看到今天之前的那一根）。
    #
    # 为什么需要这个字段：入场流程假设你**从信号月就开始盯**。但扫描器是后补进来的，
    # 它可能捞到一个 3 个月前的信号，而那个信号的入场机会早在几周前就用掉了 ——
    # 价格突破 H、又跌回 H 下方。此时系统如果还傻等"突破 H"，等的其实是**第二次突破**，
    # 那不是体系里的买点，是 C7 明令禁止的「错过初期去追高」。
    #
    # 真实案例 BAC：信号月 2026-06，H=62.66（07-27），08-12 收盘 64.48 已站上 H，
    # 到 09-18 又跌回 58.16。若不加这道闸，系统会在它重新爬回 62.66 时买入。
    h_broken_date = None
    if h_idx is not None and h_price:
        for j in range(h_idx + 1, len(daily) - 1):
            if daily[j]["c"] > h_price:
                h_broken_date = daily[j]["t"]
                break

    # 参考价与失效日
    h_date = daily[h_idx]["t"] if h_idx is not None else None
    if h_date:
        deadline = (dt.date.fromisoformat(h_date) + dt.timedelta(
            days=R["breakout_deadline_days"])).isoformat()
    elif window_open and signal_month:
        y, m = map(int, signal_month.split("-"))
        m += 1
        if m > 12:
            y, m = y + 1, 1
        deadline = (dt.date(y, m, 1) + dt.timedelta(
            days=R["breakout_deadline_days"])).isoformat()
    else:
        deadline = None

    price = (daily[-1]["c"] if daily else
             (weekly[-1]["c"] if weekly else (m_close[-1] if m_close else 0.0)))

    # 数据体检：① 日K单日跳变（拆股/复权失败的指纹）
    #           ② 各周期末值口径是否一致（月线复权、周线不复权这类最隐蔽的坏数据）
    problems: list[str] = []
    cliffs = split_cliffs(daily)
    if cliffs:
        d0, ca, cb = cliffs[0]
        problems.append(f"日K {len(cliffs)} 处单日跳变（{d0}：{ca:.2f} → {cb:.2f}）")
    gap = consistency_gap({"月": monthly, "周": weekly, "日": daily})
    if gap:
        problems.append(f"各周期末值不一致（{gap}）")
    data_ok = not problems
    data_note = ("；".join(problems) + "，疑似复权口径异常，已拒绝判定"
                 if problems else "")

    row = dict(sig)
    row.pop("sig_idx", None)
    row.update({
        "price": price,
        "data_ok": data_ok, "data_note": data_note,
        "weekly_ref": w_ref, "weekly_seq": w_seq_labelled,
        "h_price": h_price, "h_date": h_date, "h_detail": h_detail,
        "h_broken_date": h_broken_date,
        "deadline": deadline,
        "bars_ok": bool(monthly and weekly and daily),
    })
    return row


# ------------------------------------------------------------------ 决策

def _sector_count(state: dict, cfg: dict) -> dict:
    counts: dict[str, int] = {}
    for sym, pos in state["positions"].items():
        s = pos.get("sector", "—")
        counts[s] = counts.get(s, 0) + 1
    return counts


def decide(row: dict, cfg: dict, state: dict) -> list[dict]:
    """对单个标的出决策。返回动作列表。"""
    R, A = cfg["rules"], cfg["account"]
    sym, price = row["symbol"], row["price"]
    acts: list[dict] = []

    pos = state["positions"].get(sym)
    pend = state["pending"].get(sym)

    # ---------- ⓿ 数据体检闸（最高优先级） ----------
    # 宁可今天不交易，也不能拿断崖数据算出来的信号去下单。
    if not row.get("data_ok", True):
        return [{"type": "SKIP", "symbol": sym,
                 "reason": row.get("data_note", "数据异常"),
                 "detail": "数据体检不通过 → 该标的本次完全不参与判定（含持仓）"}]

    # ---------- ① 持仓管理（模块D 3.2） ----------
    if pos:
        if row["weekly_ref"] and price < row["weekly_ref"]:
            acts.append({
                "type": "SELL", "symbol": sym, "qty": pos["qty"],
                "reason": f"跌破周K有效低点 {row['weekly_ref']:.2f}",
                "detail": "模块D 3.3 周K低点规则：跌破即离场",
            })
        else:
            si = rules.stop_line(pos["entry_price"], price, R)
            pos["stop_line"] = round(si.line, 4)
            pos["stop_stage"] = si.stage
            pos["stop_label"] = si.label
            if price < si.line:
                acts.append({
                    "type": "SELL", "symbol": sym, "qty": pos["qty"],
                    "reason": f"{si.label} 止损触发（{price:.2f} < {si.line:.2f}）",
                    "detail": "模块D 4.2 三级止损，机械执行",
                })
            else:
                acts.append({
                    "type": "HOLD", "symbol": sym,
                    "reason": f"持有中｜{si.label} 止损线 {si.line:.2f}",
                    "detail": (f"周K参考价 {row['weekly_ref']:.2f}"
                               if row["weekly_ref"] else "尚无周K有效低点，按百分比止损"),
                })
        return acts

    # ---------- ② 等待入场 ----------
    if pend:
        # ⚠️ 参考价一律用**当场算出来的** row["h_price"]，不要用 state 里存的那个。
        #
        # 为什么（2026-09-19 踩到）：state 里的 h_price 是 ARM 那天写进去的快照。
        # 只要 H 的算法或数据口径有任何变化，它就变成了旧值 —— 而 decide() 是在
        # execute() 之前跑的，本轮算出的新 H 要等这一轮结束才写回 state。
        # 于是会出现「买入判据拿旧 H、报表显示新 H」的错位：
        # SCHW 旧 H=109.56 / 新 H=110.69，若股价刚好在 109.56~110.69 之间，
        # 系统就会**在没真正突破前高时买入**。这种错位不会报错，只会悄悄多买一次。
        #
        # state 里的值退化为兜底（仅当本轮 H 尚未形成时使用）。
        h_now = row.get("h_price") or pend.get("h_price")
        h_date_now = row.get("h_date") or pend.get("h_date")
        dl = row.get("deadline") or pend.get("deadline")

        if dl and row["today"] > dl:
            acts.append({
                "type": "CANCEL", "symbol": sym,
                "reason": f"{R['breakout_deadline_days']} 天未突破参考价，本次入场作废",
                "detail": "模块C C9：取消，不补仓、不死扛，等下一次状态A",
            })
            return acts
        if h_now and price > h_now:
            acts.append({
                "type": "BUY", "symbol": sym,
                "reason": f"突破参考价 {h_now:.2f}（现价 {price:.2f}）",
                "detail": f"信号月 {pend['signal_month']}｜参考价 {h_now:.2f}（{h_date_now}）",
            })
            return acts
        # H 被站上过、现在又跌回其下 → 这次机会已经用掉了，别在第二次突破时补票
        if row.get("h_broken_date"):
            acts.append({
                "type": "CANCEL", "symbol": sym,
                "reason": (f"参考价 {h_now:.2f} 已于 {row['h_broken_date']} "
                           f"被突破过，现价已回落其下 → 本次入场机会已消耗"),
                "detail": "模块C C7「错过初期不追」：只在第一次突破时买",
            })
            return acts
        acts.append({
            "type": "WAIT", "symbol": sym,
            "reason": (f"等待突破 {h_now:.2f}" if h_now
                       else "等待日K形成「冲高+回撤出绿柱」"),
            "detail": f"失效日 {dl}",
        })
        return acts

    # ---------- ③ 新信号（模块C 3.3 / 3.4） ----------
    if not (row["state_a"] and row["pre_ok"] and row["scenario"]):
        return acts

    # 后补进来的信号：入场机会可能早就用掉了。
    #
    # 能走到这里的只有一种情况 —— **突破 H 那天我们不在场**（标的还没进观察池，
    # 或者定时任务漏跑了）。既然不在场，就无法判断"突破之后已经走了多远"：
    # 现价可能只比 H 高 1%，也可能先冲到 +30% 再回到 +1%。这两种情形买进去
    # 完全是两回事，而数据里看不出区别。
    # 分不清 → 不做。这与 C7「错过初期不追」和"宁可不动，不可乱动"同一条原则。
    #
    # ⚠️ 判据是"突破过"本身，**与现价在 H 上方还是下方无关** —— 我第一版只按
    # "现价回落"过滤，结果把 CVX（现价 211.59 仍在 H=208.91 上方）的理由写成了
    # "已回落其下"，是错的。
    if row.get("h_broken_date"):
        hp = row["h_price"]
        where = "仍在其上" if price > hp else "已回落其下"
        return [{"type": "SKIP", "symbol": sym,
                 "reason": (f"入场机会已消耗：参考价 H={hp:.2f} 已于 "
                            f"{row['h_broken_date']} 被突破过，现价 {price:.2f}（{where}）"),
                 "detail": "模块C C7「错过初期不追」：突破那天不在场，无法判断已走了多远；"
                           "等下一次状态A（场景一·由绿转红）"}]

    is_reentry = (row["scenario"] == rules.SCENARIO2)
    cyc = state["cycles"].get(sym, {})
    last_stop = cyc.get("last_stop_price")

    if is_reentry:
        if last_stop is None:
            return [{"type": "SKIP", "symbol": sym,
                     "reason": "场景二但本轮无止损记录 → 属「错过初期」，不入场",
                     "detail": "模块C C7 错过就不追；提案 E-1（放开状态A 中段入场）建议不做"}]
        if R["require_reentry_above_stop"] and price <= last_stop:
            return [{"type": "SKIP", "symbol": sym,
                     "reason": f"R1 未满足：现价 {price:.2f} ≤ 上次止损价 {last_stop:.2f}",
                     "detail": "模块C R1：禁止变相摊平"}]
        if cyc.get("reentries", 0) >= R["max_reentry_per_cycle"]:
            return [{"type": "SKIP", "symbol": sym,
                     "reason": f"R3 已达上限：同轮状态A 已重入 {cyc['reentries']} 次",
                     "detail": "模块C R3：该轮作废，等下一个由绿转红"}]

    # 行业上限
    counts = _sector_count(state, cfg)
    if counts.get(row["sector"], 0) >= A["same_sector_max"]:
        return [{"type": "SKIP", "symbol": sym,
                 "reason": f"行业上限：{row['sector']} 已有 {counts[row['sector']]} 只（≤{A['same_sector_max']}）",
                 "detail": "模块D D1：同一行业 ≤2 只"}]

    acts.append({
        "type": "ARM", "symbol": sym,
        "reason": f"{row['scenario']} → 状态A 成立，进入入场观察",
        "detail": f"{row['scenario_detail']}｜{row['pre_detail']}",
    })
    return acts


# ------------------------------------------------------------------ 执行

def _size_order(broker: PaperBroker, cfg: dict, state: dict, price: float) -> tuple[float, str]:
    A = cfg["account"]
    acct = broker.account()
    equity = acct.get("equity") or cfg["account"].get("dry_equity_usd", 100000.0)
    n = A["num_positions"]
    per = equity / n
    qty = math.floor(per / price) if price > 0 else 0
    if qty * price < A["min_order_usd"]:
        return 0, f"额度 ${per:,.0f} 不足最小下单额 ${A['min_order_usd']}"
    return float(qty), f"等权重额度 ${per:,.0f} / 现价 {price:.2f} = {qty} 股"


def execute(acts: list[dict], row: dict, broker: PaperBroker,
            cfg: dict, state: dict, today: str) -> list[dict]:
    """把动作落到 state / broker，返回带执行结果的记录。"""
    done: list[dict] = []
    sym = row["symbol"]

    for a in acts:
        rec = dict(a)
        rec["ts"] = dt.datetime.now().isoformat(timespec="seconds")
        rec["price"] = row["price"]
        rec["name"] = row["name"]

        if a["type"] == "BUY":
            pend = state["pending"].get(sym, {})
            counts = _sector_count(state, cfg)
            if counts.get(row["sector"], 0) >= cfg["account"]["same_sector_max"]:
                rec["type"] = "SKIP"
                rec["reason"] = (f"行业上限：{row['sector']} 已有 "
                                 f"{counts[row['sector']]} 只（≤{cfg['account']['same_sector_max']}）")
            else:
                qty, note = _size_order(broker, cfg, state, row["price"])
                if qty <= 0:
                    rec["type"] = "SKIP"
                    rec["reason"] = note
                else:
                    res = broker.submit_market(sym, qty, "buy", a["reason"])
                    rec["order"] = res
                    rec["qty"] = qty
                    rec["detail"] = (a.get("detail", "") + "｜" + note).strip("｜")
                    cyc = state["cycles"].setdefault(sym, {
                        "cycle_id": f"{sym}-{today}", "reentries": 0,
                        "last_stop_price": None,
                    })
                    if pend.get("is_reentry"):
                        cyc["reentries"] = cyc.get("reentries", 0) + 1
                    state["positions"][sym] = {
                        "qty": qty,
                        "entry_price": round(row["price"], 4),
                        "entry_date": today,
                        "sector": row["sector"],
                        "stop_line": round(row["price"] * cfg["rules"]["stop_loss_1"], 4),
                        "stop_stage": 1,
                        "stop_label": "一级·−20%",
                        "weekly_ref": row["weekly_ref"],
                        "cycle_id": cyc["cycle_id"],
                        "signal_month": pend.get("signal_month", row["signal_month"]),
                    }
                    state["pending"].pop(sym, None)

        elif a["type"] == "SELL":
            qty = a.get("qty") or state["positions"].get(sym, {}).get("qty", 0)
            res = broker.submit_market(sym, qty, "sell", a["reason"])
            rec["order"] = res
            rec["qty"] = qty
            cyc = state["cycles"].setdefault(sym, {"reentries": 0, "last_stop_price": None})
            if "止损" in a["reason"]:
                cyc["last_stop_price"] = round(row["price"], 4)
            state["positions"].pop(sym, None)

        elif a["type"] == "ARM":
            if row["scenario"] == rules.SCENARIO1:
                # 新一轮状态A 开始 → 重置该标的重入计数与止损记录
                state["cycles"][sym] = {
                    "cycle_id": f"{sym}-{today}", "reentries": 0, "last_stop_price": None}
            state["pending"][sym] = {
                "signal_month": row["signal_month"],
                "h_price": row["h_price"],
                "h_date": row["h_date"],
                "deadline": row["deadline"],
                "is_reentry": (row["scenario"] == rules.SCENARIO2),
            }

        elif a["type"] == "CANCEL":
            state["pending"].pop(sym, None)

        elif a["type"] == "WAIT" and sym in state["pending"]:
            # 参考价随日K推进而更新
            state["pending"][sym].update({
                "h_price": row["h_price"], "h_date": row["h_date"],
                "deadline": row["deadline"],
            })

        state["history"].append({k: v for k, v in rec.items() if k != "order"})
        done.append(rec)

    return done


# ------------------------------------------------------------------ 记账

def _label(sym: str, name: str | None) -> str:
    """账本里的标的显示名。

    screen.py 从宽基池扫出来的票没有中文名，name 就是代码本身，
    直接拼会输出 "AAPL AAPL" 这种废话，这里挡掉。
    """
    name = (name or "").strip()
    return sym if (not name or name.upper() == sym.upper()) else f"{sym} {name}"


def write_ledger(rows: list[dict], records: list[dict], cfg: dict,
                 mode: str, today: str) -> Path:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    path = LEDGER_DIR / f"{today}.md"

    L: list[str] = []
    L.append(f"# AI 交易操作记录 · {today}\n")
    L.append(f"> 模式：**{mode}**"
             f"{'（dry-run，未真实下单）' if mode == 'dry' else '（Alpaca 模拟盘）'}"
             f" ｜ 体系：HOLDLE 模块C/D ｜ 观察池：{len(rows)} 只\n")
    L.append("> ⚠️ 本记录为规则执行留痕，**不构成投资建议**。\n")

    L.append("## 一、本次动作\n")
    if records:
        L.append("| 标的 | 动作 | 依据 | 价格 |")
        L.append("|---|---|---|---|")
        for r in records:
            L.append(f"| {_label(r['symbol'], r.get('name'))} | **{r['type']}** | "
                     f"{r.get('reason','')} | {r.get('price',0):.2f} |")
    else:
        L.append("_本次无动作。_\n")

    L.append("\n## 二、观察池扫描全表\n")
    L.append("| 标的 | 行业 | 数据 | 现价 | DIF | DEA | 柱 | 状态A | 前置校验 | 第一根红柱 | 入场闸门 | 周K参考价 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if not r["bars_ok"]:
            L.append(f"| {r['symbol']} | {r['sector']} | ⚠️ | — | — | — | — | ⚠️ 数据缺失 | — | — | — | — |")
            continue
        wr = f"{r['weekly_ref']:.2f}" if r["weekly_ref"] else "—"
        # 入场窗口列反映的是"闸门"（状态A ∧ 前置校验 ∧ 第一根红柱），不是"信号月是否已过"
        if r.get("gate_ok"):
            win = "🟢 已开"
        elif r["scenario"] and r["window_open"]:
            win = "⛔ 闸门未过"
        elif r["scenario"]:
            win = "⏳ 待次月"
        else:
            win = "—"
        dq = "✅" if r.get("data_ok", True) else "⛔ 断崖"
        L.append(
            f"| {_label(r['symbol'], r['name'])} | {r['sector']} | {dq} | {r['price']:.2f} | "
            f"{r['dif']:.2f} | {r['dea']:.2f} | {r['hist']:+.2f} | "
            f"{'✅' if r['state_a'] else '❌'} | "
            f"{'✅' if r['pre_ok'] else '❌'} | "
            f"{r['scenario'] or '—'} | {win} | {wr} |"
        )

    bad = [r for r in rows if r["bars_ok"] and not r.get("data_ok", True)]
    if bad:
        L.append("\n### ⛔ 数据体检未通过的标的\n")
        L.append("> 复权口径异常会让 MACD 与高低点全部失真，这些标的**本次不参与任何判定**，")
        L.append("> 已持仓的也不动（宁可不动，不可乱动）。\n")
        for r in bad:
            L.append(f"- **{r['symbol']}**：{r['data_note']}")
        L.append("")

    L.append("\n## 三、重点标的明细\n")
    for r in rows:
        if not r["bars_ok"] or not r.get("data_ok", True):
            continue
        if not (r["scenario"] or r["state_a"]):
            continue
        L.append(f"### {_label(r['symbol'], r['name'])}（{r['sector']}）\n")
        L.append(f"- 月线 MACD：DIF {r['dif']:.2f} / DEA {r['dea']:.2f} / "
                 f"柱 {r['hist']:+.2f}（上月 {r['hist_prev']:+.2f}）")
        L.append(f"- 状态A：{'成立' if r['state_a'] else '不成立'}｜"
                 f"前置校验：{'通过' if r['pre_ok'] else '不通过'}（{r['pre_detail']}）")
        L.append(f"- 第一根红柱：{r['scenario'] or '无'}｜{r['scenario_detail']}")
        hp = f"{r['h_price']:.2f}" if r["h_price"] else "尚未形成"
        if r.get("gate_ok"):
            gate_line = "🟢 已开启（三项闸门全部通过）"
        elif r["scenario"]:
            gate_line = f"⛔ 未通过 —— {r.get('gate_reason','')}"
        else:
            gate_line = "未开启"
        L.append(f"- 入场闸门：{gate_line}｜参考价 H：{hp}")
        L.append(f"- H 说明：{r['h_detail']}｜失效日：{r['deadline'] or '—'}")
        if r["weekly_ref"]:
            seq = " → ".join(f"{v:.2f}" for _, v in r["weekly_seq"])
            L.append(f"- 周K有效低点序列：{seq}（最新参考价 {r['weekly_ref']:.2f}）")
        L.append("")

    L.append("---\n")
    L.append("*规则实现：`rules.py`｜主引擎：`engine.py`｜参数：`config.json`*")
    L.append("*方法来源：HOLDLE 公开课程，版权归原作者所有。**不构成投资建议**。*")

    path.write_text("\n".join(L), encoding="utf-8")

    # JSONL 流水
    jl = LEDGER_DIR / "trades.jsonl"
    with jl.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return path
