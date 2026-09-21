"""
选股扫描器：在宽基候选池里自己找票，不必人工先圈定一小撮标的。

背景：`config.json` 的 watchlist 是人工「选好」的产物，数量有限。想扩大到
几百只的宽基池、让程序按规则自己去挑，就需要一个能批量粗筛的东西。

两级设计（为了省 API 调用 —— 免费档只有 200 次/分钟）：

    第一级（便宜）：只取**月线**。状态A + 前置校验 + 第一根红柱 三个判据全部只用
                    月线数据，所以 180 只票 = 180 次请求就够。
                    走 `engine.monthly_signal()`，和全量分析共用同一份代码。
    第二级（昂贵）：只对第一级通过的少数几只取周线 + 日线，跑 `engine.analyze()`
                    全量分析（含数据体检、周K有效低点、日K参考价 H）。

用法：
    python screen.py                     只扫描，出报告（不下单、不改配置）
    python screen.py --apply             把通过闸门的标的并入 config.json 的 watchlist
    python screen.py --apply --prune     顺带清掉已失效的（本脚本加进去的）标的
    python screen.py --selftest          跑合并逻辑自检（不需要网络）

可选：
    --limit 30        只扫前 30 只（调试用）
    --sleep 0.32      两次请求之间的最小间隔（秒），默认 0.32 ≈ 180 次/分钟
    --universe X.json 换一个候选池
    --config Y.json   换一个配置文件
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engine                                          # noqa: E402
from alpaca_io import MarketData                       # noqa: E402

UNIVERSE_FILE = HERE / "universe.json"
CONFIG_FILE = HERE / "config.json"
SCREEN_DIR = HERE / "screens"


# ------------------------------------------------------------------ 候选池

def load_universe(path: Path = UNIVERSE_FILE) -> list[dict]:
    """把按行业分组的 universe.json 展开成 [{symbol, name, sector}, ...]。"""
    blob = json.loads(Path(path).read_text(encoding="utf-8"))
    sectors = blob.get("sectors") or {}
    out: list[dict] = []
    seen: set[str] = set()
    for sector, syms in sectors.items():
        for s in syms:
            sym = str(s).strip().upper()
            if not sym or sym in seen:
                continue          # 同一只票写重了不要被扫两次
            seen.add(sym)
            out.append({"symbol": sym, "name": sym, "sector": sector})
    return out


def _ym(date_str: str) -> int:
    """'2026-09-18' 或 '2026-09' → 2026*12+9，用来算相差几个月。"""
    return int(date_str[:4]) * 12 + int(date_str[5:7])


def is_stale(monthly: list[dict], today: str, max_gap_months: int = 1) -> bool:
    """最后一根月K 是否已经太旧（多半是退市 / 停止交易 / 数据源断了）。

    为什么必须挡：ANSS（ANSYS）2025 年被新思收购退市，月线停在 2025-07。
    它的「最近 4 根柱体」里当然还留着一个漂亮的第一根红柱 —— 于是扫描器会
    兴冲冲地把一只**已经不能交易的股票**报成买点。这种错误不会报错，只会让你
    对着一个查不到的代码发呆。
    """
    if not monthly:
        return True
    return (_ym(today) - _ym(monthly[-1]["t"])) > max_gap_months


def eligible(full: list[dict], today: str) -> tuple[list[dict], list[tuple[dict, str]]]:
    """把「闸门过了」和「现在真能买」区分开。

    闸门（状态A ∧ 前置校验 ∧ 第一根红柱）只管**信号**成不成立，不管这个信号
    还有没有效、这只票还买不买得到。两道过滤：

      ① 失效期已过 —— 失效期规则：H 之后 60 天没突破，本次入场作废。
         典型是 PANW：H=367.50，现价 374.99 已经在 H 上方，但失效日 09-04 早过了。
         现在追进去就是「错过初期不追」明令禁止的追高。
      ② 日K数据陈旧 —— 价格都取不到新的，谈不上「突破」。
    """
    ok: list[dict] = []
    bad: list[tuple[dict, str]] = []
    for r in full:
        if not r.get("data_ok", True):
            bad.append((r, f"数据体检不通过：{r.get('data_note', '')}"))
            continue
        dl = r.get("deadline")
        if dl and today > dl:
            bad.append((r, f"失效期已过（{dl}，失效期规则）"))
            continue
        # 入场机会已消耗：H 被站上过、又跌回来了。
        # 典型是 BAC：信号月 2026-06，H=62.66（07-27），08-12 收盘 64.48 已站上，
        # 到 09-18 又跌回 58.16。这时系统若还当它是"等待突破"，等的是第二次突破。
        if r.get("h_broken_date"):
            where = "现价仍在其上" if r["price"] > r["h_price"] else "现价已回落其下"
            bad.append((r, f"入场机会已消耗（H={r['h_price']:.2f} 已于 "
                           f"{r['h_broken_date']} 被突破过，{where}）"))
            continue
        ok.append(r)
    return ok, bad


# ------------------------------------------------------------------ 扫描

def scan(items: list[dict], md: MarketData, cfg: dict, today: str,
         log=print) -> tuple[list[dict], list[dict], list[tuple[str, str]],
                              list[tuple[dict, str]]]:
    """返回 (第一级全表, 第二级通过闸门且可交易的, 取数异常, 闸门过了但不可交易)。"""
    rows: list[dict] = []
    errs: list[tuple[str, str]] = []

    for i, item in enumerate(items, 1):
        sym = item["symbol"]
        try:
            monthly = md.bars(sym, "month")
            if is_stale(monthly, today):
                if monthly:
                    errs.append((sym, f"行情已停止更新（最后月K {monthly[-1]['t']}）"
                                      f"，疑似退市，已跳过"))
                continue
            rows.append(engine.monthly_signal(item, monthly, cfg, today))
        except Exception as e:                                  # noqa: BLE001
            errs.append((sym, f"第一级取数失败：{str(e)[:100]}"))
        if i % 25 == 0 or i == len(items):
            log(f"  … 第一级已扫 {i}/{len(items)}")

    # 第二级：只对闸门全过的少数几只补周线 + 日线
    by_sym = {x["symbol"]: x for x in items}
    gated: list[dict] = []
    for r in [r for r in rows if r["gate_ok"]]:
        sym = r["symbol"]
        try:
            gated.append(engine.analyze(
                by_sym[sym], md.bars(sym, "month"), md.bars(sym, "week"),
                md.bars(sym, "day"), cfg, today))
        except Exception as e:                                  # noqa: BLE001
            errs.append((sym, f"第二级失败：{str(e)[:100]}"))

    ok, bad = eligible(gated, today)
    return rows, ok, errs, bad


# ------------------------------------------------------------------ 合并 watchlist

def merge_watchlist(cfg: dict, candidates: list[dict], state: dict,
                    prune: bool = False,
                    sector_map: dict[str, str] | None = None
                    ) -> tuple[list[dict], list[str], list[str],
                               list[tuple[str, str, str]]]:
    """把候选并入 watchlist。返回 (新 watchlist, 新增, 移除, 行业标签修正)。

    三条不可协商的安全约束：

      ① **已有持仓 / 正在等待入场的标的永不删除。** 删了就等于系统第二天不再扫它，
         仓位立刻变成无人看守的裸仓 —— 止损、周K低点规则全部失效。这是最危险的一种
         "清理"，所以锁死。
      ② 人工维护的核心标的（没有 `source` 标记的）**永不自动删除**。程序不替人做
         删库的决定。
      ③ 只有本脚本自己加进去的（`source == "screen"`）、且当前**没有任何活信号**的，
         才允许在 --prune 时移除。

    行业标签修正：核心池里 MCD 标的是「消费」、COST 也是「消费」，而宽基池按 GICS
    分为「可选消费 / 日常消费」。混用会让「同一行业 ≤2 只」这条约束算错（把可乐和
    麦当劳当成一个行业）。所以以宽基池的 GICS 标签为准，顺手把核心池对齐 ——
    只改标签，不动任何别的字段。

    ⚠️ `sector_map` 必须是**整池**的代码→行业表，不能只传 candidates。
    传 candidates 的话，只有「今天恰好通过闸门」的那几只会被对齐，
    像 COST/MCD 这种没信号的永远不会被修 —— 这个 bug 我第一版就写出来了。
    """
    wl = [dict(w) for w in cfg.get("watchlist", [])]
    if sector_map is None:
        sector_map = {c["symbol"]: c.get("sector", "—") for c in candidates}
    sector_of = dict(sector_map)
    have = {w["symbol"] for w in wl}
    locked = set(state.get("positions", {})) | set(state.get("pending", {}))
    live = {c["symbol"] for c in candidates}

    # --- 行业标签修正（按整池口径）---
    fixed: list[tuple[str, str, str]] = []
    for w in wl:
        new = sector_of.get(w["symbol"])
        if new and new != w.get("sector"):
            fixed.append((w["symbol"], w.get("sector", "—"), new))
            w["sector"] = new

    # --- 新增 ---
    added: list[str] = []
    for c in candidates:
        if c["symbol"] in have:
            continue
        wl.append({
            "symbol": c["symbol"],
            "name": c.get("name") or c["symbol"],
            "sector": c.get("sector", "—"),
            "source": "screen",
            "added": c.get("today"),
        })
        have.add(c["symbol"])
        added.append(c["symbol"])

    # --- 清理 ---
    removed: list[str] = []
    if prune:
        kept = []
        for w in wl:
            s = w["symbol"]
            if w.get("source") == "screen" and s not in locked and s not in live:
                removed.append(s)
                continue
            kept.append(w)
        wl = kept

    return wl, added, removed, fixed


# ------------------------------------------------------------------ 报告

def render_md(rows: list[dict], full: list[dict], errs: list[tuple[str, str]],
              rejected: list[tuple[dict, str]], cfg: dict, today: str,
              n_universe: int, changes: dict | None, md: MarketData) -> str:
    L: list[str] = []
    L.append(f"# 选股扫描报告 · {today}\n")
    L.append(f"> 候选池 {n_universe} 只 ｜ 网络请求 {md.requests} 次 ｜ "
             f"体系：趋势跟随体系 ｜ 池子：`universe.json`\n")
    L.append("> ⚠️ 本报告为规则筛选留痕，**不构成投资建议**。\n")

    n_a = sum(1 for r in rows if r["state_a"])
    n_p = sum(1 for r in rows if r["pre_ok"])
    n_s = sum(1 for r in rows if r["scenario"])
    # 这里必须是 len(full)（已过失效期/数据体检两道过滤），不能是 gate_ok 的计数 ——
    # 否则漏斗最后一行会写 9 而下面明细只有 8 行，自相矛盾。
    n_g = len(full)

    L.append("## 一、漏斗\n")
    L.append("| 环节 | 判据 | 通过 |")
    L.append("|---|---|---|")
    L.append(f"| 候选池 | `universe.json` | {n_universe} |")
    L.append(f"| ① 状态A | 月线 DIF>0 ∧ DEA>0 ∧ 柱>0 | {n_a} |")
    L.append(f"| ② 前置校验 | 月K低点逐月抬高 ∧ 高点逐月抬高 | {n_p} |")
    L.append(f"| ③ 第一根红柱 | 由绿转红 ∨ 由矮变高（≥上月×1.10） | {n_s} |")
    L.append(f"| 闸门过了但不可交易 | 失效期已过 / 数据体检不过 | −{len(rejected)} |")
    L.append(f"| **入场闸门** | **①∧②∧③ 且仍有效** | **{n_g}** |")
    L.append("")
    L.append("> 漏斗越往下越窄是正常的。月线级别的状态A 本来就少见，")
    L.append("> 三项同时成立是「等好」的全部意义 —— 大部分时间本来就应该空仓。\n")

    L.append("## 二、通过入场闸门的标的（全量分析）\n")
    if not full:
        L.append("_本次无标的通过入场闸门。_\n")
    else:
        L.append("| 标的 | 行业 | 现价 | 场景 | 参考价 H | 距 H | 失效日 | 数据 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in full:
            h = r["h_price"]
            h_txt = f"{h:.2f}" if h else "待形成"
            gap = f"{(r['price'] / h - 1) * 100:+.2f}%" if h else "—"
            dq = "✅" if r.get("data_ok", True) else "⛔ 断崖"
            L.append(f"| {r['symbol']} | {r['sector']} | {r['price']:.2f} | "
                     f"{r['scenario'] or '—'} | {h_txt} | {gap} | "
                     f"{r['deadline'] or '—'} | {dq} |")
        L.append("")

    L.append("## 三、闸门过了、但不可交易（已排除）\n")
    if not rejected:
        L.append("_无。_\n")
    else:
        L.append("| 标的 | 行业 | 现价 | 参考价 H | 排除原因 |")
        L.append("|---|---|---|---|---|")
        for r, why in rejected:
            h_txt = f"{r['h_price']:.2f}" if r.get("h_price") else "待形成"
            L.append(f"| {r['symbol']} | {r['sector']} | {r['price']:.2f} | "
                     f"{h_txt} | {why} |")
        L.append("")
        L.append("> 「失效期已过」不是 bug —— 失效期规则规定 H 之后 50–60 天不突破即作废，")
        L.append("> 现在追进去就是「错过初期不追」明令禁止的追高。\n")

    L.append("## 四、接近的标的（有信号但闸门未全过）\n")
    near = [r for r in rows if (not r["gate_ok"]) and (r["scenario"] or (r["state_a"] and r["pre_ok"]))]
    if not near:
        L.append("_无。_\n")
    else:
        L.append("| 标的 | 行业 | 柱 | 状态A | 前置校验 | 第一根红柱 | 差在哪 |")
        L.append("|---|---|---|---|---|---|---|")
        for r in sorted(near, key=lambda x: (x["scenario"] is None, x["symbol"])):
            L.append(f"| {r['symbol']} | {r['sector']} | {r['hist']:+.3f} | "
                     f"{'✅' if r['state_a'] else '❌'} | "
                     f"{'✅' if r['pre_ok'] else '❌'} | "
                     f"{r['scenario'] or '—'} | {r['gate_reason']} |")
        L.append("")

    if changes:
        L.append("## 五、watchlist 变更\n")
        if changes["added"]:
            L.append(f"- **新增 {len(changes['added'])} 只**："
                     f"{'、'.join(changes['added'])}")
        if changes["removed"]:
            L.append(f"- **移除 {len(changes['removed'])} 只**（信号已失效）："
                     f"{'、'.join(changes['removed'])}")
        if changes["fixed"]:
            L.append("- 行业标签修正：")
            for sym, old, new in changes["fixed"]:
                L.append(f"  - `{sym}`：{old} → {new}")
        if not (changes["added"] or changes["removed"] or changes["fixed"]):
            L.append("- 无变化。")
        L.append(f"- watchlist 现有 **{changes['total']}** 只\n")

    if errs:
        L.append("## 六、取数异常 / 已跳过\n")
        for sym, msg in errs:
            L.append(f"- `{sym}`：{msg}")
        L.append("")

    L.append("---\n")
    L.append("*规则实现：`rules.py`｜月线判定：`engine.monthly_signal`｜"
             "全量分析：`engine.analyze`*")
    L.append("*方法来源：HOLDLE 公开课程，版权归原作者所有。**不构成投资建议**。*")
    return "\n".join(L)


# ------------------------------------------------------------------ 自检

def _selftest() -> None:
    ok = fail = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✅ {name}")
        else:
            fail += 1
            print(f"  ❌ {name} {extra}")

    base = {
        "watchlist": [
            {"symbol": "MSFT", "name": "微软", "sector": "信息技术"},
            {"symbol": "COST", "name": "好市多", "sector": "消费"},
            {"symbol": "MCD", "name": "麦当劳", "sector": "消费"},
        ]
    }
    state = {
        "positions": {"MCD": {"qty": 10}},
        "pending": {"FAST": {"h_price": 52.9}},
    }

    print("── 新增 ──")
    cands = [
        {"symbol": "AAPL", "sector": "信息技术", "today": "2026-09-18"},
        {"symbol": "MSFT", "sector": "信息技术", "today": "2026-09-18"},   # 已存在
    ]
    wl, added, removed, fixed = merge_watchlist(base, cands, state)
    syms = [w["symbol"] for w in wl]
    check("新标的被加入", "AAPL" in syms)
    check("已存在的不会重复加", syms.count("MSFT") == 1, f"{syms}")
    check("新增清单只列真的新加的", added == ["AAPL"], f"{added}")
    check("未 prune 时不删任何东西", removed == [])

    print("── 行业标签修正 ──")
    cands2 = [
        {"symbol": "COST", "sector": "日常消费", "today": "2026-09-18"},
        {"symbol": "MCD", "sector": "可选消费", "today": "2026-09-18"},
    ]
    wl2, _, _, fixed2 = merge_watchlist(base, cands2, state)
    by = {w["symbol"]: w for w in wl2}
    check("COST 标签被修正", by["COST"]["sector"] == "日常消费")
    check("MCD 标签被修正", by["MCD"]["sector"] == "可选消费")
    check("修正被记录", ("COST", "消费", "日常消费") in fixed2, f"{fixed2}")
    check("未列在候选池里的不动", by["MSFT"]["sector"] == "信息技术")

    print("── 行业标签修正必须按整池口径 ──")
    # 回归：第一版只拿 candidates 当行业表，结果只有"今天恰好过闸门"的票会被修，
    # COST/MCD 这种没信号的永远修不到。必须显式传整池的 sector_map。
    base5 = {"watchlist": [{"symbol": "COST", "name": "好市多", "sector": "消费"}]}
    wl5, _, _, fixed5 = merge_watchlist(
        base5, [], {}, sector_map={"COST": "日常消费"})
    check("★ 没有信号的票也能被修（传整池表）",
          wl5[0]["sector"] == "日常消费", f"{wl5}")
    wl6, _, _, fixed6 = merge_watchlist(base5, [], {})
    check("★ 只传 candidates 时修不到（正是要避免的 bug）",
          wl6[0]["sector"] == "消费" and fixed6 == [])

    print("── 清理（安全约束）──")
    cfg3 = {
        "watchlist": [
            {"symbol": "MSFT", "name": "微软", "sector": "信息技术"},
            {"symbol": "AAPL", "sector": "信息技术", "source": "screen"},
            {"symbol": "NKE", "sector": "可选消费", "source": "screen"},
            {"symbol": "MCD", "name": "麦当劳", "sector": "可选消费", "source": "screen"},
            {"symbol": "FAST", "name": "Fastenal", "sector": "工业", "source": "screen"},
        ]
    }
    cands3 = [{"symbol": "AAPL", "sector": "信息技术", "today": "2026-09-18"}]
    wl3, _, removed3, _ = merge_watchlist(cfg3, cands3, state, prune=True)
    syms3 = [w["symbol"] for w in wl3]
    check("失效的 screen 标的被移除", "NKE" in removed3, f"{removed3}")
    check("仍有活信号的保留", "AAPL" in syms3)
    check("核心标的永不删", "MSFT" in syms3)
    check("★ 有持仓的永不删（MCD）", "MCD" in syms3, f"{syms3}")
    check("★ 等待入场的永不删（FAST）", "FAST" in syms3, f"{syms3}")
    check("移除清单不含锁定的", "MCD" not in removed3 and "FAST" not in removed3)

    print("── 空配置 ──")
    wl4, added4, removed4, fixed4 = merge_watchlist({}, cands2, {}, prune=True)
    check("空 watchlist 不炸", [w["symbol"] for w in wl4] == ["COST", "MCD"], f"{wl4}")
    check("空 state 下 prune 不留残留", removed4 == [])

    print("── 陈旧数据识别（退市标的）──")
    today = "2026-09-18"
    check("当月月K → 不算陈旧", not is_stale([{"t": "2026-09-01"}], today))
    check("上月月K → 不算陈旧", not is_stale([{"t": "2026-08-01"}], today))
    check("★ 停在去年 → 判定陈旧（ANSS 型）",
          is_stale([{"t": "2025-07-01"}], today))
    check("空序列 → 判定陈旧", is_stale([], today))

    print("── 可交易性过滤（失效期）──")
    live = {"symbol": "JPM", "sector": "金融", "price": 349.39, "h_price": 366.30,
            "deadline": "2026-10-12", "data_ok": True}
    dead = {"symbol": "PANW", "sector": "信息技术", "price": 374.99, "h_price": 367.50,
            "deadline": "2026-09-04", "data_ok": True}
    dirty = {"symbol": "KLAC", "sector": "信息技术", "price": 168.05, "h_price": None,
             "deadline": None, "data_ok": False, "data_note": "日K 1 处单日跳变"}
    okx, badx = eligible([live, dead, dirty], today)
    check("有效期内的留下", [r["symbol"] for r in okx] == ["JPM"], f"{okx}")
    check("★ 失效期已过的排除（PANW）", any(r["symbol"] == "PANW" for r, _ in badx))
    check("★ 数据体检不过的排除（KLAC）", any(r["symbol"] == "KLAC" for r, _ in badx))
    check("排除理由写明失效日",
          any("2026-09-04" in w for _, w in badx), f"{badx}")
    check("失效日恰好是今天 → 仍算有效（today > dl 才作废）",
          eligible([dict(live, deadline="2026-09-18")], today)[0] != [])
    check("无失效日 → 保留", eligible([dict(live, deadline=None)], today)[0] != [])

    print(f"\n自检结果：{ok} 通过 / {fail} 失败")
    if fail:
        raise SystemExit(1)


# ------------------------------------------------------------------ CLI

def main() -> None:
    ap = argparse.ArgumentParser(description="HOLDLE 体系 · 宽基选股扫描")
    ap.add_argument("--apply", action="store_true", help="并入 config.json 的 watchlist")
    ap.add_argument("--prune", action="store_true", help="顺带清掉失效的 screen 标的")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.32)
    ap.add_argument("--universe", default=str(UNIVERSE_FILE))
    ap.add_argument("--config", default=str(CONFIG_FILE))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return

    items = load_universe(Path(args.universe))
    if args.limit:
        items = items[:args.limit]
    cfg_path = Path(args.config)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    today = dt.date.today().isoformat()

    print(f"── HOLDLE 选股扫描 ｜ {today} ｜ 候选池 {len(items)} 只 ──\n")
    md = MarketData(cfg["data"]["feed"], min_interval=args.sleep)
    rows, full, errs, rejected = scan(items, md, cfg, today)
    state = engine.load_state()

    changes = None
    if args.apply:
        wl, added, removed, fixed = merge_watchlist(
            cfg, [{"symbol": r["symbol"], "name": r["name"], "sector": r["sector"],
                   "today": today} for r in full], state, prune=args.prune,
            sector_map={x["symbol"]: x["sector"] for x in items})
        cfg["watchlist"] = wl
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        changes = {"added": added, "removed": removed, "fixed": fixed,
                   "total": len(wl)}

    report = render_md(rows, full, errs, rejected, cfg, today, len(items), changes, md)
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    out = SCREEN_DIR / f"{today}.md"
    out.write_text(report, encoding="utf-8")

    if args.json:
        print(json.dumps({"today": today, "universe": len(items),
                          "state_a": [r["symbol"] for r in rows if r["state_a"]],
                          "gate_ok": [r["symbol"] for r in full],
                          "changes": changes}, ensure_ascii=False, indent=2))

    print(f"\n── 第一级：{len(rows)} 只有月线数据 ｜ "
          f"状态A {sum(1 for r in rows if r['state_a'])} 只 ｜ "
          f"闸门全过 {len(full)} 只（已排除 {len(rejected)} 只失效/异常）──")
    for r in full:
        h = r["h_price"]
        gap = f"{(r['price'] / h - 1) * 100:+.2f}%" if h else "—"
        print(f"  🟢 {r['symbol']:6s} {r['price']:>9.2f}  {r['scenario']}  "
              f"H={h if h else '待形成'}  距H {gap}  失效 {r['deadline']}")
    for r, why in rejected:
        print(f"  ⛔ {r['symbol']:6s} {why}")
    if changes:
        print(f"\n── watchlist：新增 {changes['added'] or '无'} ｜ "
              f"移除 {changes['removed'] or '无'} ｜ 现 {changes['total']} 只 ──")
    print(f"\n报告：{out}")
    if not args.apply:
        print("（未改配置。要并入观察池请加 --apply）")


if __name__ == "__main__":
    main()
