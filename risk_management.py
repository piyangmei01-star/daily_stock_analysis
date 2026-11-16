# -*- coding: utf-8 -*-
"""风控模块 risk_management.py —— 确定性规则计算，非机器学习、非预测概率、非投资建议。

设计目标（2026-11-16）
  1. ATR(14) 动态止损：基于同一条 OHLC 序列算 TR 的 14 日均值；给出 ATR 参考止损、
     结构止损（近 N 日最低 - 缓冲）、以及最终【建议止损价】= 三者中最靠近现价者，
     并被【最大 8% 亏损硬卡控】兜底（止损价不得低于 现价*(1-8%)，即亏损超过 8% 一律收紧到 8%）。
  2. 大盘环境仓位评估：基于 成交额 / 涨跌家数比 / 指数均线位置 三因子，输出建议仓位档位。
  3. 严格防御性编程：所有计算与数据获取全部 try-except 包裹；数据缺失时平稳降级输出
     默认兜底值；模块级 safe_* 入口绝不抛异常，绝不中断原有选股/推送流程。

与项目现有纪律一致
  - 复权口径绝不混用：一次调用只采用一个数据源的 K 线（东财 fqt=1 前复权 / 新浪不复权），
    meta 显式标注 source / adjustment / kline_data_date；日K取数逻辑复用已验证草案。
  - 缺失字段存 None，绝不填假值；ATR=0 / 一字板 / 脏 bar 均优雅处理，不除零。
  - 本模块【不修改、不调用、不打断】任何推送主链路；调用方按需 import 并自行决定是否展示。
  - 所有输出仅为规则参考位与仓位档位，非买卖建议，不触发任何交易。

对外 API
  compute_atr_stops(bars, entry_price, ...) -> dict   # 纯计算，无网络
  evaluate_position(breadth, turnover, indices, ...) -> dict  # 纯计算，无网络
  analyze_stock_risk(code, entry_price, ...) -> dict  # 取数+计算，全程兜底
  risk_report_line(result) -> str                     # 展示层，None -> "不适用"
"""
import json
import os
import time

import requests

# ---- 固定参数（写死以保证可复现，不随运行时刻漂移）----
ATR_PERIOD = 14          # ATR 周期（TR 的 14 日简单均值）
STRUCT_LOOKBACK = 20     # 结构止损回看窗口（近 20 日最低价）
STRUCT_BUFFER_PCT = 1.0  # 结构止损在最低价下方留 1% 缓冲
MAX_LOSS_PCT = 8.0       # 最大 8% 亏损硬卡控
ATR_STOP_MULT = 2.0      # ATR 参考止损 = 现价 - 2*ATR14
MIN_BARS = 25            # 至少需能算 MA20 + ATR14
KLINE_LMT = 80
EM_TIMEOUT = 6
SINA_TIMEOUT = 8
KLINE_BUDGET_SEC = 45

EM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
              "Referer": "https://quote.eastmoney.com/"}
SINA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://finance.sina.com.cn/"}
KLINE_SINA_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_t=/"
                  "CN_MarketDataService.getKLineData")
EM_HIS_HOSTS = ["push2his.eastmoney.com", "33.push2his.eastmoney.com"]
SOURCE_ORDER = ("eastmoney", "sina")


# ---- 通用小工具（全部防御性）----
def _num(v):
    """安全转 float；None/空/非法 -> None，绝不抛异常。"""
    try:
        if v is None or v == "" or v == "-" or isinstance(v, bool):
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None  # 排除 NaN/inf
    except (TypeError, ValueError, OverflowError):
        return None


def _show(v, suffix=""):
    """展示层：None -> '不适用'，绝不替换成 0 或任何假值。"""
    return "不适用" if v is None else f"{v}{suffix}"


def _round(v, n=2):
    try:
        return None if v is None else round(float(v), n)
    except (TypeError, ValueError):
        return None


def _market(code):
    """返回 (东财 secid, 新浪 symbol)；非沪深A股/非法代码 -> None（不编造、不发错误请求）。"""
    try:
        code = str(code).strip()
        if not (len(code) == 6 and code.isdigit()):
            return None
        if code.startswith("6"):
            return ("1." + code, "sh" + code)
        if code.startswith(("0", "3")):
            return ("0." + code, "sz" + code)
        return None  # 北交所 4/8 开头等暂不支持
    except Exception:
        return None


def _valid_bar(o, c, h, l):
    """OHLC 自洽校验：脏 bar 直接丢弃，不参与计算、不修补成假值。"""
    try:
        if None in (o, c, h, l):
            return False
        if h < max(o, c) or l > min(o, c) or h < l:
            return False
        if min(o, c, h, l) <= 0:
            return False
        return True
    except Exception:
        return False


def _source_order():
    try:
        raw = os.environ.get("KLINE_SOURCE_ORDER", "")
        order = tuple(x.strip() for x in raw.split(",") if x.strip() in SOURCE_ORDER)
        return order or SOURCE_ORDER
    except Exception:
        return SOURCE_ORDER


# ---- 日K取数（复用已验证草案逻辑，全程 try-except）----
def _fetch_eastmoney(secid, lmt, deadline, errors):
    for host in EM_HIS_HOSTS:
        if time.monotonic() >= deadline:
            break
        try:
            remain = deadline - time.monotonic()
            r = requests.get(
                f"https://{host}/api/qt/stock/kline/get",
                params={"secid": secid, "klt": "101", "fqt": "1", "beg": "0",
                        "end": "20500101", "fields1": "f1",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57", "lmt": str(lmt)},
                headers=EM_HEADERS, timeout=max(2, min(EM_TIMEOUT, int(remain))))
            ks = ((r.json().get("data") or {}).get("klines") or [])
            bars = []
            for line in ks:
                try:
                    p = str(line).split(",")
                    if len(p) < 5:
                        continue
                    o, c, h, l = (_num(p[1]), _num(p[2]), _num(p[3]), _num(p[4]))
                    if not _valid_bar(o, c, h, l):
                        continue
                    bars.append({"day": p[0], "open": o, "high": h,
                                 "low": l, "close": c, "volume": _num(p[5])})
                except Exception:
                    continue
            if len(bars) >= MIN_BARS:
                return bars, {"source": "eastmoney_kline_fqt1", "adjustment": "qfq"}
            errors.append(f"{host}: 有效K线不足({len(bars)})")
        except Exception as e:
            errors.append(f"{host}: {type(e).__name__}")
    return [], {}


def _fetch_sina(sym, lmt, deadline, errors):
    for attempt in range(2):
        if time.monotonic() >= deadline:
            break
        try:
            remain = deadline - time.monotonic()
            r = requests.get(KLINE_SINA_URL,
                             params={"symbol": sym, "scale": "240", "ma": "no",
                                     "datalen": str(lmt)},
                             headers=SINA_HEADERS,
                             timeout=max(2, min(SINA_TIMEOUT, int(remain))))
            t = r.text
            d = json.loads(t[t.find("(") + 1:t.rfind(")")])
            bars = []
            for x in d or []:
                try:
                    o, c, h, l = (_num(x.get("open")), _num(x.get("close")),
                                  _num(x.get("high")), _num(x.get("low")))
                    if not _valid_bar(o, c, h, l):
                        continue
                    bars.append({"day": x.get("day"), "open": o, "high": h,
                                 "low": l, "close": c, "volume": _num(x.get("volume"))})
                except Exception:
                    continue
            if len(bars) >= MIN_BARS:
                return bars, {"source": "sina_kline_day", "adjustment": "none"}
            errors.append(f"sina: 有效K线不足({len(bars)})")
        except Exception as e:
            errors.append(f"sina: {type(e).__name__}")
    return [], {}


def fetch_ohlc_kline(code, lmt=KLINE_LMT, source_order=None):
    """返回 (bars, meta)；失败返回 ([], meta_ok_false)，绝不抛异常。"""
    meta = {"ok": False, "source": None, "adjustment": None,
            "kline_data_date": None, "bars_count": 0, "error": None,
            "budget_exceeded": False}
    try:
        mk = _market(code)
        if mk is None:
            meta["error"] = "unsupported_code(非沪深A股或代码非法)"
            return [], meta
        em_secid, sina_sym = mk
        deadline = time.monotonic() + KLINE_BUDGET_SEC
        errors = []
        fetchers = {"eastmoney": lambda: _fetch_eastmoney(em_secid, lmt, deadline, errors),
                    "sina": lambda: _fetch_sina(sina_sym, lmt, deadline, errors)}
        order = source_order or _source_order()
        for src in order:
            try:
                if time.monotonic() >= deadline:
                    meta["budget_exceeded"] = True
                    break
                fn = fetchers.get(src)
                if fn is None:
                    continue
                bars, got = fn()
                if bars:
                    meta.update(ok=True, source=got["source"],
                                adjustment=got["adjustment"],
                                kline_data_date=bars[-1]["day"], bars_count=len(bars))
                    return bars, meta
            except Exception as e:
                errors.append(f"{src}: {type(e).__name__}")
        if time.monotonic() >= deadline:
            meta["budget_exceeded"] = True
        meta["error"] = " | ".join(errors) or "两源均未返回足够K线"
    except Exception as e:
        meta["error"] = f"fetch_ohlc_kline: {type(e).__name__}"
    return [], meta


# ---- 核心计算1：ATR(14) 动态止损 + 结构止损 + 8% 硬卡控 ----
def compute_atr_stops(bars, entry_price, atr_period=ATR_PERIOD,
                      lookback=STRUCT_LOOKBACK, max_loss_pct=MAX_LOSS_PCT,
                      atr_mult=ATR_STOP_MULT):
    """纯计算（无网络）：由 OHLC 序列与入场价，给出三层止损与最终建议止损价。
    全程 try-except，任何异常/数据不足 -> 返回 ok=False 的兜底结构，绝不抛异常。

    建议止损价逻辑（取三者中最高/最靠近现价者，亏损最小优先）：
      - ATR 参考止损 = entry - atr_mult * ATR14
      - 结构止损 = 近 lookback 日最低 - 缓冲%；且必须低于 entry 才有效
      - 8% 硬卡线 = entry * (1 - max_loss_pct/100)   # 亏损上限，止损不得低于此价
      最终 = max(有效候选..., 8%硬卡线)；若连硬卡线都算不出 -> None。
    """
    out = {"ok": False, "atr14": None, "atr_pct": None,
           "atr_stop": None, "struct_stop": None, "hard_floor": None,
           "suggested_stop": None, "max_loss_pct": None,
           "basis_close": None, "data_date": None, "error": None}
    try:
        entry = _num(entry_price)
        if entry is None or entry <= 0:
            out["error"] = "invalid_entry_price"
            return out
        if not isinstance(bars, (list, tuple)) or len(bars) < MIN_BARS:
            out["error"] = f"insufficient_bars({len(bars) if bars else 0}<{MIN_BARS})"
            return out
        period = int(atr_period) if _num(atr_period) and int(atr_period) > 0 else ATR_PERIOD
        lb = int(lookback) if _num(lookback) and int(lookback) > 0 else STRUCT_LOOKBACK
        cap = _num(max_loss_pct)
        cap = cap if (cap is not None and 0 < cap < 100) else MAX_LOSS_PCT
        mult = _num(atr_mult)
        mult = mult if (mult is not None and mult > 0) else ATR_STOP_MULT

        closes, trs = [], []
        prev_close = None
        for b in bars:
            try:
                h, l, c = _num(b.get("high")), _num(b.get("low")), _num(b.get("close"))
                if not _valid_bar(_num(b.get("open")), c, h, l):
                    continue
                closes.append(c)
                if prev_close is not None:
                    tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
                    trs.append(tr)
                prev_close = c
            except Exception:
                continue
        if len(trs) < period or not closes:
            out["error"] = f"insufficient_tr({len(trs)}<{period})"
            return out

        atr14 = sum(trs[-period:]) / float(period)
        basis_close = closes[-1]
        out.update(ok=True, basis_close=_round(basis_close),
                   data_date=str(bars[-1].get("day")) if bars else None)
        out["atr14"] = _round(atr14, 3)
        out["atr_pct"] = _round(atr14 / basis_close * 100, 2) if basis_close else None

        # ATR 参考止损
        atr_stop = entry - mult * atr14 if atr14 > 0 else None
        out["atr_stop"] = _round(atr_stop)

        # 结构止损：近 lb 日最低 - 缓冲%，且必须低于入场价
        lows = [_num(b.get("low")) for b in bars[-lb:]]
        lows = [x for x in lows if x is not None and x > 0]
        struct_stop = None
        if lows:
            cand = min(lows) * (1 - STRUCT_BUFFER_PCT / 100.0)
            if cand < entry:
                struct_stop = cand
        out["struct_stop"] = _round(struct_stop)

        # 8% 硬卡线（亏损上限）
        hard_floor = entry * (1 - cap / 100.0)
        out["hard_floor"] = _round(hard_floor)

        # 最终建议：候选取最靠近现价（最高）者，再被硬卡线兜底（不得低于硬卡线）
        cands = [x for x in (atr_stop, struct_stop) if x is not None and x < entry]
        suggested = max(cands) if cands else hard_floor
        if suggested < hard_floor:
            suggested = hard_floor          # 超过最大亏损，一律收紧到硬卡线
        suggested = min(suggested, entry)   # 止损不得高于入场价
        out["suggested_stop"] = _round(suggested)
        out["max_loss_pct"] = _round((entry - suggested) / entry * 100, 2) if entry else None
        return out
    except Exception as e:
        out["error"] = f"compute_atr_stops: {type(e).__name__}"
        return out


# ---- 核心计算2：大盘环境仓位评估（成交额 / 涨跌家数比 / 均线位置）----
def evaluate_position(breadth=None, turnover=None, indices=None):
    """纯计算（无网络）：三因子打分 -> 建议仓位档位。全程 try-except，失败返回中性兜底。

    breadth: {adv, dec, amount_yi} 涨跌家数与两市成交额（亿元）
    turnover: {ratio} 成交额环比（今日/昨日）
    indices:  {"上证指数": {"close":..,"ma20":..} / 或 {"bias_pct":..}, ...} 指数均线位置
    输出: {ok, score, level, suggested_position_pct, reasons[], factors{}}
    仓位档位: 进攻(80%) / 均衡(60%) / 谨慎(40%) / 防守(20%) / 数据不足(30%保守)
    """
    out = {"ok": False, "score": 0, "level": "数据不足",
           "suggested_position_pct": 30, "reasons": [],
           "factors": {"breadth": None, "turnover": None, "ma": None}, "error": None}
    try:
        score, reasons = 0, []
        factors = out["factors"]

        # 因子1：涨跌家数比（adv/(adv+dec)）
        adv = _num((breadth or {}).get("adv"))
        dec = _num((breadth or {}).get("dec"))
        if adv is not None and dec is not None and (adv + dec) > 0:
            br = adv / (adv + dec)
            factors["breadth"] = _round(br, 3)
            if br >= 0.65:
                score += 2; reasons.append("涨跌家数比偏强")
            elif br >= 0.5:
                score += 1; reasons.append("涨跌家数比中性")
            elif br >= 0.35:
                score -= 1; reasons.append("涨跌家数比偏弱")
            else:
                score -= 2; reasons.append("涨跌家数比弱势")

        # 因子2：成交额环比
        ratio = _num((turnover or {}).get("ratio"))
        if ratio is not None and ratio > 0:
            factors["turnover"] = _round(ratio, 3)
            if ratio >= 1.1:
                score += 1; reasons.append("成交额放量")
            elif ratio >= 0.9:
                score += 0; reasons.append("成交额持平")
            else:
                score -= 1; reasons.append("成交额缩量")

        # 因子3：指数均线位置（收盘 vs MA20）
        idx = indices if isinstance(indices, dict) else {}
        biases = []
        for label, d in idx.items():
            try:
                d = d or {}
                bias = _num(d.get("bias_pct"))
                close, ma20 = _num(d.get("close")), _num(d.get("ma20"))
                if bias is None and close is not None and ma20 not in (None, 0):
                    bias = (close - ma20) / ma20 * 100
                if bias is not None:
                    biases.append(bias)
            except Exception:
                continue
        if biases:
            avg_bias = sum(biases) / len(biases)
            factors["ma"] = _round(avg_bias, 2)
            if avg_bias >= 1.0:
                score += 2; reasons.append("指数站上MA20")
            elif avg_bias >= -1.0:
                score += 0; reasons.append("指数贴近日线均线")
            else:
                score -= 2; reasons.append("指数跌破MA20")

        have = sum(1 for v in (factors["breadth"], factors["turnover"], factors["ma"])
                   if v is not None)
        out.update(ok=have > 0, score=score, reasons=reasons)
        # 档位映射（分数越高越进攻）
        if score >= 4:
            out.update(level="进攻", suggested_position_pct=80)
        elif score >= 2:
            out.update(level="均衡", suggested_position_pct=60)
        elif score >= 0:
            out.update(level="谨慎", suggested_position_pct=40)
        else:
            out.update(level="防守", suggested_position_pct=20)
        # 数据严重不足时，保守固定 30%，不因单项噪声给出激进档
        if have < 2:
            out.update(level="数据不足(保守)", suggested_position_pct=30)
        return out
    except Exception as e:
        out["error"] = f"evaluate_position: {type(e).__name__}"
        return out


# ---- 组合入口：取数 + 计算，全程兜底（safe_* 语义）----
def analyze_stock_risk(code, entry_price, source_order=None):
    """单股风控分析：取 K 线 -> 算 ATR/结构/8%止损。任何失败都返回 ok=False 兜底，绝不抛异常。"""
    result = {"ok": False, "code": str(code), "entry_price": _num(entry_price),
              "stops": compute_atr_stops([], entry_price), "meta": None, "error": None}
    try:
        bars, meta = fetch_ohlc_kline(code, source_order=source_order)
        result["meta"] = meta
        if not bars:
            result["error"] = meta.get("error") or "kline_unavailable"
            return result
        stops = compute_atr_stops(bars, entry_price)
        result["stops"] = stops
        result["ok"] = bool(stops.get("ok"))
        if not result["ok"]:
            result["error"] = stops.get("error")
        return result
    except Exception as e:
        result["error"] = f"analyze_stock_risk: {type(e).__name__}"
        return result


def safe_market_position(breadth=None, turnover=None, indices=None):
    """仓位评估的安全包装：即使入参是脏数据也绝不抛异常。"""
    try:
        return evaluate_position(breadth, turnover, indices)
    except Exception as e:
        return {"ok": False, "score": 0, "level": "数据不足",
                "suggested_position_pct": 30, "reasons": [],
                "factors": {}, "error": f"safe_market_position: {type(e).__name__}"}


# ---- 展示层 ----
def risk_report_line(result):
    """把 analyze_stock_risk 结果转成一行文本（None -> '不适用'）；绝不抛异常。"""
    try:
        stops = (result or {}).get("stops") or {}
        parts = [f"ATR14 {_show(stops.get('atr14'))}",
                 f"建议止损 {_show(stops.get('suggested_stop'))}",
                 f"最大亏损 {_show(stops.get('max_loss_pct'), '%')}",
                 f"结构止损 {_show(stops.get('struct_stop'))}"]
        return " | ".join(parts)
    except Exception:
        return "风控数据: 不适用"


def position_report_line(result):
    """把仓位评估结果转成一行文本；绝不抛异常。"""
    try:
        r = result or {}
        return (f"大盘仓位建议: {r.get('level', '数据不足')} "
                f"({_show(r.get('suggested_position_pct'), '%')})；"
                + "；".join(r.get("reasons") or ["无依据"]))
    except Exception:
        return "大盘仓位建议: 数据不足"


# ---- 供主程序调用的聚合展示（只进 md/归档，不碰微信推送；整体防御）----
def market_position_md_lines(breadth=None, turnover=None, indices=None):
    """生成「大盘仓位」Markdown 段（列表行）。任何异常都降级为一行提示，绝不抛。
    breadth/turnover/indices 由调用方复用已取好的真实数据传入，本函数不再联网。"""
    try:
        pos = safe_market_position(breadth=breadth, turnover=turnover, indices=indices)
        title = "## 🧭 大盘环境仓位评估（规则参考，非投资建议）"
        line = f"- **{position_report_line(pos)}**"
        detail = (f"- 评分 {_show(pos.get('score'))}｜因子齐备 "
                  f"{sum(1 for v in (pos.get('factors') or {}).values() if v is not None)}/3")
        return [title, "", line, detail]
    except Exception:
        return ["## 🧭 大盘环境仓位评估", "", "- 数据不足，暂不给仓位建议（不编造）"]


def stops_md_table(picks):
    """批量生成「个股止损价」Markdown 表格行。picks: [{code,name,entry}, ...]。
    entry 为入场价（通常取当日真实收盘价）；逐股调用 safe 入口，单股失败只降级该行。"""
    try:
        lines = ["## 🛡️ 个股风控止损参考（ATR14 + 结构止损 + 8% 硬卡控）", "",
                 "| 代码 | 名称 | 入场价 | ATR14 | 建议止损 | 最大亏损 | 结构止损 | 数据状态 |",
                 "|---|---|---:|---:|---:|---:|---:|---|"]
        for p in (picks or []):
            try:
                code = str((p or {}).get("code") or "").strip()
                name = (p or {}).get("name") or code
                entry = _num((p or {}).get("entry"))
                if not code:
                    continue
                res = analyze_stock_risk(code, entry)
                stops = (res or {}).get("stops") or {}
                if res.get("ok"):
                    status = "真实"
                    atr = _show(stops.get("atr14"))
                    sug = _show(stops.get("suggested_stop"))
                    loss = _show(stops.get("max_loss_pct"), "%")
                    struct = _show(stops.get("struct_stop"))
                else:
                    status = f"降级({res.get('error') or '无数据'})"
                    atr = sug = loss = struct = "不适用"
                lines.append(f"| {code} | {name} | {_show(entry)} | {atr} | {sug} | "
                             f"{loss} | {struct} | {status} |")
            except Exception as e:
                try:
                    nm = (p or {}).get("name") or (p or {}).get("code") or "未知"
                    lines.append(f"| {_show((p or {}).get('code'))} | {nm} | 不适用 | "
                                 f"不适用 | 不适用 | 不适用 | 不适用 | 降级({type(e).__name__}) |")
                except Exception:
                    pass
                continue
        lines.append("")
        lines.append("> 规则参考位，非买卖建议；8% 硬卡控确保任何止损不突破最大亏损 8% 红线。")
        return lines
    except Exception:
        return ["## 🛡️ 个股风控止损参考", "", "- 数据不足，暂不给出止损参考（不编造）"]


if __name__ == "__main__":
    # 轻量自检：纯计算，不联网
    demo = compute_atr_stops(
        [{"day": f"d{i}", "open": 10, "high": 11, "low": 9,
          "close": 10 + (i % 3) - 1, "volume": 1000} for i in range(MIN_BARS)],
        entry_price=10.0)
    print("ATR自检:", json.dumps(demo, ensure_ascii=False))
    pos = safe_market_position({"adv": 3000, "dec": 1500}, {"ratio": 1.05}, {})
    print("仓位自检:", json.dumps(pos, ensure_ascii=False))
