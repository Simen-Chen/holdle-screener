"""
Alpaca 数据源 + 模拟盘下单封装。

⚠️ 密钥只从环境变量读，绝不写入任何文件：
    ALPACA_API_KEY / ALPACA_SECRET_KEY
    （模拟盘的 key 在 Alpaca 后台 "Paper" 页签生成，和实盘 key 是两套）

未装 alpaca-py 或未配密钥时，本模块会抛出带说明的异常；
engine.py 会据此提示，不会静默失败。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "state" / "cache"
CACHE_TTL_HOURS = 6

# 复权口径。这个值会写进缓存文件；一旦口径变了，旧缓存自动作废 ——
# 避免"改了取数方式却还在用老数据"这种最阴险的坑。
ADJUSTMENT_TAG = "all"


# ------------------------------------------------------------------ 密钥

def _win_env_from_registry(name: str) -> str:
    """Windows 兜底：直接去注册表读用户级环境变量。

    为什么需要这一层：`setx` 写的是注册表，只有**在它之后启动**的进程才会继承到
    进程环境。定时任务如果是由一个早于 setx 启动的常驻父进程拉起来的，就读不到
    —— 结果是每天早上静默失败、一笔都不交易，而且不会报错给你看。

    多这一层兜底，密钥仍然只存在注册表里（不落盘到任何项目文件），但读得到了。
    """
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for root, sub in locations:
        try:
            with winreg.OpenKey(root, sub) as key:
                val, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if val:
            return str(val).strip()
    return ""


def _read_secret(name: str) -> tuple[str, str]:
    """返回 (值, 来源说明)。先看进程环境，再兜底查注册表。"""
    v = (os.environ.get(name) or "").strip()
    if v:
        return v, "进程环境变量"
    v = _win_env_from_registry(name)
    if v:
        return v, "注册表（用户级）"
    return "", "未找到"


def get_keys() -> tuple[str, str]:
    k, k_src = _read_secret("ALPACA_API_KEY")
    s, s_src = _read_secret("ALPACA_SECRET_KEY")
    if not k or not s:
        missing = [n for n, v in (("ALPACA_API_KEY", k),
                                  ("ALPACA_SECRET_KEY", s)) if not v]
        raise RuntimeError(
            f"未检测到 Alpaca 密钥（缺 {'、'.join(missing)}）。请先设置环境变量：\n"
            "  setx ALPACA_API_KEY  <你的 Paper Key>\n"
            "  setx ALPACA_SECRET_KEY <你的 Paper Secret>\n"
            "（设置后需重开终端生效。Paper Key 在 Alpaca 后台 Paper 页签生成）"
        )
    return k, s


def key_sources() -> dict[str, str]:
    """诊断用：报告每个密钥是从哪儿读到的（不返回值本身）。"""
    return {n: _read_secret(n)[1]
            for n in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")}


def has_keys() -> bool:
    try:
        get_keys()
        return True
    except RuntimeError:
        return False


# ------------------------------------------------------------------ 行情

_TIMEFRAMES = {"month": "Month", "week": "Week", "day": "Day"}

_LOOKBACK_DAYS = {"month": 60 * 32, "week": 160 * 7, "day": 400}


def _cache_path(symbol: str, tf: str) -> Path:
    return CACHE_DIR / f"{symbol}_{tf}.json"


def _load_cache(symbol: str, tf: str):
    p = _cache_path(symbol, tf)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if blob.get("adjustment") != ADJUSTMENT_TAG:
        return None                      # 复权口径变了 → 旧缓存作废
    try:
        fetched = dt.datetime.fromisoformat(blob["fetched_at"])
    except Exception:
        return None
    if dt.datetime.now() - fetched > dt.timedelta(hours=CACHE_TTL_HOURS):
        return None
    return blob["bars"]


def _save_cache(symbol: str, tf: str, bars: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(symbol, tf).write_text(
        json.dumps({"fetched_at": dt.datetime.now().isoformat(),
                    "adjustment": ADJUSTMENT_TAG, "bars": bars},
                   ensure_ascii=False),
        encoding="utf-8",
    )


CLIFF_TOL = 1.5      # 单日跳变 ≥50%：正常行情几乎不可能，基本只可能是拆股/复权失败
XTF_TOL = 0.05       # 各周期末值允许 5% 偏差


def split_cliffs(bars: list[dict], tol: float = CLIFF_TOL) -> list[tuple[str, float, float]]:
    """找出复权失败留下的假断崖：相邻两根K的收盘价跳变 ≥ tol 倍。

    ⚠️ 只应该喂**日K**。拆股在日线上必然表现为单日跳空；而月线上一根 −39% 的
    月K可能是真实的暴跌（KLAC 2026-07 就是），拿月线判会把真行情误杀。

    返回 [(日期, 前收, 本收), ...]。一旦非空，说明复权口径出了问题，
    **绝不能拿它算指标下单**。
    """
    out: list[tuple[str, float, float]] = []
    for a, b in zip(bars, bars[1:]):
        ca, cb = a.get("c", 0.0), b.get("c", 0.0)
        if ca > 0 and cb > 0 and max(ca / cb, cb / ca) >= tol:
            out.append((b.get("t", "?"), ca, cb))
    return out


def consistency_gap(series: dict[str, list[dict]], tol: float = XTF_TOL) -> str:
    """校验各周期的末值是否落在同一价格口径上。

    同一天的各周期最后一根K，收盘价必须基本一致。若月线是复权价、周线却是
    未复权价（Alpaca 上真实发生过），两者会差出好几倍 —— 这是最隐蔽的一种坏数据，
    单看任何一个周期都发现不了。
    """
    vals = {tf: b[-1]["c"] for tf, b in series.items() if b}
    if len(vals) < 2:
        return ""
    lo, hi = min(vals.values()), max(vals.values())
    if lo <= 0 or hi / lo <= 1 + tol:
        return ""
    return "；".join(f"{tf}末收 {v:.2f}" for tf, v in vals.items())


class MarketData:
    """Alpaca 行情。免费档默认走 IEX feed。

    min_interval：两次网络请求之间的最小间隔（秒）。默认 0，即不限速 ——
    观察池只有 20 来只时没必要。`screen.py` 一次要扫 180+ 只，会把免费档的
    200 次/分钟配额顶满，那时才需要把它调成 0.32 之类。
    """

    def __init__(self, feed: str = "iex", use_cache: bool = True,
                 min_interval: float = 0.0):
        self.feed = feed
        self.use_cache = use_cache
        self.min_interval = min_interval
        self._client = None
        self._last_call = 0.0
        self.requests = 0          # 真实打到网络的次数（缓存命中不算）

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        gap = dt.datetime.now().timestamp() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)

    def _fetch(self, req, retries: int = 3):
        """带限速与退避重试的取数。免费档超配额会抛 429，直接崩掉整轮扫描太亏。"""
        last: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            try:
                self._last_call = dt.datetime.now().timestamp()
                self.requests += 1
                return self._ensure_client().get_stock_bars(req)
            except Exception as e:                              # noqa: BLE001
                msg = str(e).lower()
                last = e
                if "429" not in msg and "rate limit" not in msg and "too many" not in msg:
                    raise
                time.sleep(2.0 * (attempt + 1))
        raise last if last else RuntimeError("行情获取失败")

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from alpaca.data.historical import StockHistoricalDataClient
        except ImportError as e:
            raise RuntimeError(
                "未安装 alpaca-py。请先执行：python -m pip install alpaca-py"
            ) from e
        key, secret = get_keys()
        self._client = StockHistoricalDataClient(key, secret)
        return self._client

    def bars(self, symbol: str, tf: str) -> list[dict]:
        """返回 [{t, o, h, l, c, v}, ...]，按时间升序。"""
        if tf not in _TIMEFRAMES:
            raise ValueError(f"不支持的周期：{tf}")

        if self.use_cache:
            cached = _load_cache(symbol, tf)
            if cached is not None:
                return cached

        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed

        start = dt.datetime.now() - dt.timedelta(days=_LOOKBACK_DAYS[tf])
        kwargs = dict(
            symbol_or_symbols=[symbol],
            timeframe=getattr(TimeFrame, _TIMEFRAMES[tf]),
            start=start,
        )
        # ⚠️ 必须显式要复权数据。Alpaca 默认 adjustment='raw'（不复权），
        # 一旦期间发生拆股，历史K线就会出现假断崖 —— 例如 AVGO 2024-07 的 10:1，
        # 不复权时周线最高价是 1850，而现价只有 339；KLAC 2026-06 的 10:1，
        # 同一根月K里开盘 1898、最低 235。MACD 建在这种序列上全是垃圾值。
        try:
            from alpaca.data.enums import Adjustment
            kwargs["adjustment"] = Adjustment.ALL     # 拆股 + 分红 全复权
        except Exception:
            pass
        try:
            kwargs["feed"] = getattr(DataFeed, self.feed.upper())
            req = StockBarsRequest(**kwargs)
        except Exception:
            kwargs.pop("feed", None)
            req = StockBarsRequest(**kwargs)

        resp = self._fetch(req)
        raw = resp.data.get(symbol, []) if hasattr(resp, "data") else []

        bars = [
            {
                "t": b.timestamp.date().isoformat(),
                "o": float(b.open), "h": float(b.high),
                "l": float(b.low), "c": float(b.close),
                "v": float(getattr(b, "volume", 0) or 0),
            }
            for b in raw
        ]
        bars.sort(key=lambda x: x["t"])

        if self.use_cache and bars:
            _save_cache(symbol, tf, bars)
        return bars


# ------------------------------------------------------------------ 交易

class PaperBroker:
    """Alpaca 模拟盘。mode='dry' 时只记录意图，不真的下单。"""

    def __init__(self, mode: str = "dry"):
        self.mode = mode
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as e:
            raise RuntimeError(
                "未安装 alpaca-py。请先执行：python -m pip install alpaca-py"
            ) from e
        key, secret = get_keys()
        # paper=True 是硬编码的：本脚本只允许连模拟盘
        self._client = TradingClient(key, secret, paper=True)
        return self._client

    def account(self) -> dict:
        if self.mode == "dry":
            return {"equity": None, "cash": None, "dry": True}
        acct = self._ensure_client().get_account()
        return {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "dry": False,
        }

    def positions(self) -> dict:
        if self.mode == "dry":
            return {}
        out = {}
        for p in self._ensure_client().get_all_positions():
            out[p.symbol] = {
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "market_value": float(p.market_value),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
        return out

    # ---------------------------------------------------------- 业绩报告

    def portfolio_history(self, period: str = "1M",
                          timeframe: str = "1D") -> dict:
        """净值曲线。返回 {"base_value": float, "points": [{t, equity, pl, plpc}, ...]}。

        plpc 由 equity / base_value 自行推算 —— 不用 Alpaca 的 profit_loss_pct 字段，
        因为那个字段的量纲（分数还是百分数）在不同版本间改过，自己算不会有歧义。
        """
        if self.mode == "dry":
            return {"base_value": 0.0, "points": []}
        from alpaca.trading.requests import GetPortfolioHistoryRequest

        h = self._ensure_client().get_portfolio_history(
            GetPortfolioHistoryRequest(period=period, timeframe=timeframe))
        base = float(h.base_value or 0.0)
        ts = list(h.timestamp or [])
        eq = list(h.equity or [])
        pl = list(h.profit_loss or [])

        points = []
        for i, t in enumerate(ts):
            e = float(eq[i]) if i < len(eq) and eq[i] is not None else None
            # 账户开户前的点会返回 equity=0，那不是"亏光了"而是"还没数据"，
            # 混进曲线里会被读成 −100% 回撤，必须滤掉。
            if not e or e <= 0:
                continue
            points.append({
                "t": dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(),
                "equity": e,
                "pl": (float(pl[i]) if i < len(pl) and pl[i] is not None else None),
                "plpc": ((e / base - 1.0) * 100.0) if base else None,
            })
        return {"base_value": base, "points": points}

    def orders(self, limit: int = 200, after: dt.datetime | None = None) -> list[dict]:
        """成交/委托记录（Alpaca 侧的真实凭据）。"""
        if self.mode == "dry":
            return []
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        kw = dict(status=QueryOrderStatus.ALL, limit=limit, direction="asc")
        if after is not None:
            kw["after"] = after
        try:
            raw = self._ensure_client().get_orders(GetOrdersRequest(**kw))
        except Exception:
            return []
        out = []
        for o in raw:
            out.append({
                "symbol": str(o.symbol),
                "side": str(getattr(o.side, "value", o.side)),
                "qty": float(o.qty or 0),
                "filled_qty": float(o.filled_qty or 0),
                "filled_avg_price": (float(o.filled_avg_price)
                                     if o.filled_avg_price is not None else None),
                "status": str(getattr(o.status, "value", o.status)),
                "submitted_at": (o.submitted_at.isoformat()
                                 if getattr(o, "submitted_at", None) else None),
            })
        return out

    def submit_market(self, symbol: str, qty: float, side: str, note: str = "") -> dict:
        """市价单。side = buy / sell。"""
        if qty <= 0:
            return {"status": "skipped", "reason": "数量为 0"}

        if self.mode == "dry":
            return {
                "status": "dry-run",
                "symbol": symbol, "qty": qty, "side": side,
                "note": note,
                "ts": dt.datetime.now().isoformat(timespec="seconds"),
            }

        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self._ensure_client().submit_order(req)
        return {
            "status": "submitted",
            "order_id": str(order.id),
            "symbol": symbol, "qty": qty, "side": side,
            "note": note,
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
        }


# ------------------------------------------------------------------ 自检

if __name__ == "__main__":
    print("alpaca-py 是否已装：", end="")
    try:
        import alpaca  # noqa: F401
        print("是")
    except ImportError:
        print("否 —— 请执行 python -m pip install alpaca-py")

    src = key_sources()
    print("密钥读取来源：")
    for name, where in src.items():
        mark = "✅" if where != "未找到" else "❌"
        print(f"  {mark} {name}：{where}")
    if all(v == "未找到" for v in src.values()):
        print("\n→ 需设置 ALPACA_API_KEY / ALPACA_SECRET_KEY（README 第二节）")
