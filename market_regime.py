# -*- coding: utf-8 -*-
"""market_regime — 大盘环境与情绪温控模块（2026-11-15 新增，用户明确授权）

嵌入方式（早/晚双循环，均不影响核心选股链路）
  晚间 daily_ai_top10.py: 复盘报告与推送头部增加【内生趋势与情绪定调】三档
  早间 quality_screen_v2.py: 推送头部增加【隔夜外围与排雷风控】温控红绿灯

设计纪律
  1. 定调/红绿灯由【确定性规则】计算（阈值写死、可审计），DeepSeek 只做文本解释
     （宏观利空排雷 + 一句话情绪定调），不改变红绿灯，不是预测模型、不产生概率。
  2. 任一数据源失败 → 该项记 None 并显示"数据不足"，绝不填假值、绝不编造；
     定调规则对缺失项容忍（缺失项不作为否决条件；关键项全缺时输出"数据不足不定调"）。
  3. 所有公开函数内部兜底，绝不向上抛异常 → 绝不影响选股主链路与时序。
  4. 原始数据、规则依据（evidence）全部随归档留痕，可审计、可复盘。
  5. akshare 接口在境外可能无限挂起（财联社 cls 实测挂 >90s），所有 akshare 调用
     一律经 _with_timeout 硬超时保护（30s），超时按数据不足处理。

数据源（2026-11-15 沙箱实测，runner 上以部署后验证为准）
  ✓ ak.stock_zh_index_daily（新浪）上证/沪深300 日K → MA20 / 乖离率
  ✓ ak.stock_zt_pool_em / ak.stock_zt_pool_dtgc_em（东财 push2ex）涨停 / 跌停数
  ✓ market_data.fetch_snapshot（东财 clist → 新浪 Market_Center，runner 已验证）
    → 全市场涨跌家数、两市成交额（Σamount）
  ✓ 新浪 hq.sinajs.cn: hf_CHA50CFD（富时A50期指）/ gb_$hxc（纳斯达克中国金龙指数）
    / fx_susdcnh（离岸人民币 USD/CNH）
  ✓ ak.stock_info_global_sina（新浪全球快讯；财联社 cls 接口境外挂起不可控，故用新浪快讯，
    来源如实标注为"新浪全球快讯"）
  ✓ DeepSeek（runner secrets OPENAI_API_KEY，与 05 晚间同通道；未配置 Key 时跳过并如实标注）
  ✗ 指数成交额环比：东财 push2his / 腾讯 ifzq / 乐咕 沙箱均断流 → 东财→腾讯双源尝试，
    都失败时环比项显示"数据不足"，不否决定调（今日成交额绝对值仍由快照 Σamount 给出）。
"""
import json
import os
import threading
import time

import requests

try:
    import akshare as ak
except Exception:                       # akshare 缺失时模块仍可加载（新浪系接口不依赖它）
    ak = None

try:
    import market_data as md            # 同目录，runner 已验证的全市场通道
except Exception:
    md = None

SINA_HQ_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                   "Referer": "https://finance.sina.com.cn/"}
SINA_HQ_URL = "https://hq.sinajs.cn/list="

# ---------------- 规则阈值（写死、可审计；均为确定性规则，非预测概率） ----------------
BREADTH_BULL = 0.55      # 涨/(涨+跌) >= 0.55 视为普涨
BREADTH_BEAR = 0.40      # < 0.40 视为普跌
TURN_MIN = 0.90          # 成交额环比下限：多头定调要求 >= 0.90（缺失不否决）
A50_RED, A50_GREEN = -0.8, 0.3       # A50 期指涨跌幅红/绿阈值（%）
HXC_RED, HXC_GREEN = -2.0, 0.0       # 纳斯达克中国金龙指数阈值（%）
CNH_RED, CNH_GREEN = 0.3, 0.1        # USDCNH 涨幅即人民币贬值幅度阈值（%）
NEWS_MAX = 12            # 送入 DeepSeek 的快讯条数
NEWS_ITEM_CHARS = 80     # 每条快讯截断长度
AK_TIMEOUT_SEC = 30      # akshare 硬超时（防境外挂起拖垮任务）
AI_TIMEOUT = 45          # DeepSeek 超时
_TIMEOUT = object()      # 硬超时哨兵：必须用 `is` 判断（DataFrame 不能与字符串 == 比较）


def _show(v, suffix=""):
    return "数据不足" if v is None else (str(v) if not suffix else f"{v}{suffix}")


# ---------------------------------------------------------------- 通用工具
def _with_timeout(fn, seconds=AK_TIMEOUT_SEC, *args):
    """硬超时执行：akshare 在境外可能无限挂起，超时返回 None（按数据不足处理）。"""
    box = {}

    def _run():
        try:
            box["v"] = fn(*args)
        except Exception as e:
            box["e"] = e

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        return _TIMEOUT
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _sina_quotes(codes):
    """新浪实时行情批量。返回 {code: raw_string}；失败返回 {}。"""
    try:
        r = requests.get(SINA_HQ_URL + ",".join(codes), headers=SINA_HQ_HEADERS, timeout=10)
        out = {}
        for line in r.text.splitlines():
            m = line.split('="')
            if len(m) < 2:
                continue
            key = m[0].split("hq_str_")[-1].strip()   # 保留完整变量名（如 hf_CHA50CFD）
            out[key] = m[1].rstrip('";').strip()
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------- 新浪解析（字段序按 2026-11-15 实测样本）
def _parse_a50(raw):
    """hf_CHA50CFD: [0]现价 [6]时间 [7]昨收 [12]日期 [13]名称"""
    if not raw:
        return None
    s = raw.split(",")
    if len(s) < 13:
        return None
    try:
        price, prev = float(s[0]), float(s[7])
        return {"price": price, "prev_close": prev,
                "pct": round((price / prev - 1) * 100, 2) if prev else None,
                "time": s[6], "date": s[12], "name": s[13] if len(s) > 13 else "富时中国A50期货"}
    except (ValueError, IndexError):
        return None


def _parse_hxc(raw):
    """gb_$hxc: [0]名称 [1]现价 [2]涨跌幅% [3]时间"""
    if not raw:
        return None
    s = raw.split(",")
    if len(s) < 4:
        return None
    try:
        return {"name": s[0], "price": float(s[1]), "pct": float(s[2]), "time": s[3]}
    except (ValueError, IndexError):
        return None


def _parse_cnh(raw):
    """fx_susdcnh: [0]时间 [3]昨收 [8]最新 [9]名称 [10]涨跌额 [11]涨跌幅(小数比例)。
    pct>0 表示美元升值/人民币贬值（对A股为利空方向）。"""
    if not raw:
        return None
    s = raw.split(",")
    if len(s) < 12:
        return None
    try:
        price, prev = float(s[8]), float(s[3])
        pct = round((price / prev - 1) * 100, 2) if prev else None
        pct_alt = round(float(s[11]) * 100, 2)   # 新浪自带涨跌幅，交叉留痕
        return {"price": price, "prev_close": prev, "pct": pct, "pct_sina": pct_alt,
                "time": s[0], "name": s[9], "date": s[16] if len(s) > 16 else None,
                "raw": raw[:120]}
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------- 晚间：内生趋势与情绪
def _index_ma20(sym, label):
    """新浪指数日K → 收盘/MA20/乖离率。"""
    if ak is None:
        return {"label": label, "error": "akshare不可用"}
    try:
        df = _with_timeout(ak.stock_zh_index_daily, AK_TIMEOUT_SEC, sym)
        if df is None or df is _TIMEOUT or len(df) < 21:
            return {"label": label, "error": "K线不足或超时"}
        close = float(df["close"].iloc[-1])
        ma20 = float(df["close"].tail(20).mean())
        return {"label": label, "close": round(close, 2), "ma20": round(ma20, 2),
                "bias_pct": round((close / ma20 - 1) * 100, 2),
                "kline_date": str(df["date"].iloc[-1])}
    except Exception as e:
        return {"label": label, "error": type(e).__name__}


def _limit_counts():
    """东财涨停池/跌停池 → (涨停数, 跌停数)，日期取最近交易日。"""
    if ak is None or md is None:
        return None, None
    try:
        day = md.expected_data_date()[0].replace("-", "")
        zt_df = _with_timeout(ak.stock_zt_pool_em, AK_TIMEOUT_SEC, day)
        dt_df = _with_timeout(ak.stock_zt_pool_dtgc_em, AK_TIMEOUT_SEC, day)
        zt = len(zt_df) if zt_df is not None and zt_df is not _TIMEOUT else None
        dt = len(dt_df) if dt_df is not None and dt_df is not _TIMEOUT else None
        return zt, dt
    except Exception:
        return None, None


def _breadth_and_amount():
    """全市场快照 → 涨/跌/平家数、两市成交额（Σamount，元）。复用 runner 已验证通道。"""
    if md is None:
        return None
    df, meta = md.fetch_snapshot()      # 失败抛 MarketDataError，由上层兜底
    pct = df["pct"].dropna()
    adv = int((pct > 0).sum())
    dec = int((pct < 0).sum())
    flat = int((pct == 0).sum())
    amount = round(float(df["amount"].dropna().sum()), 0)
    return {"adv": adv, "dec": dec, "flat": flat,
            "amount_yuan": amount,
            "amount_yi": round(amount / 1e8, 0),
            "snapshot_source": meta.get("source"),
            "data_date": meta.get("data_date"),
            "quote_basis": meta.get("quote_basis")}


def _turnover_ratio():
    """两市成交额环比（今日/昨日）。东财 push2his / 腾讯 均可能断流，失败返回 None。
    akshare 内部无超时，故用 _with_timeout 保护。"""
    if ak is None:
        return None
    try:
        def _em():
            d1 = ak.stock_zh_index_daily_em(symbol="sh000001",
                                            start_date="20261101", end_date="20500101")
            d2 = ak.stock_zh_index_daily_em(symbol="sz399001",
                                            start_date="20261101", end_date="20500101")
            a1 = float(d1["amount"].iloc[-1]) + float(d2["amount"].iloc[-1])
            a0 = float(d1["amount"].iloc[-2]) + float(d2["amount"].iloc[-2])
            return {"ratio": round(a1 / a0, 3), "today_yi": round(a1 / 1e8, 0),
                    "prev_yi": round(a0 / 1e8, 0), "source": "eastmoney_index_amount"}
        r = _with_timeout(_em, AK_TIMEOUT_SEC)
        if r is None or r is _TIMEOUT:
            return None
        return r
    except Exception:
        return None


def _classify_evening(idx, breadth, limits, tratio):
    """三档定调：确定性规则，先多头、再退潮、其余震荡；缺失项不作为否决条件。"""
    ev = []
    sse = (idx or {}).get("上证指数") or {}
    hs3 = (idx or {}).get("沪深300") or {}
    sb, hb = sse.get("bias_pct"), hs3.get("bias_pct")
    adv, dec = (breadth or {}).get("adv"), (breadth or {}).get("dec")
    br = (adv / (adv + dec)) if (adv is not None and dec is not None and adv + dec > 0) else None
    zt, dt = limits if limits else (None, None)

    if sb is not None:
        ev.append(f"上证乖离{sb:+.2f}%")
    if hb is not None:
        ev.append(f"沪深300乖离{hb:+.2f}%")
    if br is not None:
        ev.append(f"涨跌比{br:.2f}")
    if zt is not None and dt is not None:
        ev.append(f"涨停{zt}/跌停{dt}")
    tr = (tratio or {}).get("ratio")
    if tr is not None:
        ev.append(f"成交额环比{tr}")
    else:
        ev.append("成交额环比数据不足")

    bull = (sb is not None and sb > 0 and hb is not None and hb > 0
            and br is not None and br >= BREADTH_BULL
            and (zt is None or dt is None or zt >= dt)
            and (tratio is None or tratio.get("ratio", 0) >= TURN_MIN))
    bear = ((sb is not None and sb < 0 and hb is not None and hb < 0)
            or (br is not None and br < BREADTH_BEAR)
            or (zt is not None and dt is not None and dt > zt and (br is None or br < 0.55)))
    # 证据全缺：不定调
    if sb is None and hb is None and br is None and zt is None:
        return {"regime": None, "label": "数据不足不定调", "evidence": ev}
    if bull:
        return {"regime": "bull", "label": "多头进攻", "evidence": ev}
    if bear:
        return {"regime": "bear", "label": "退潮防守", "evidence": ev}
    return {"regime": "shake", "label": "震荡分化", "evidence": ev}


def evening_regime():
    """晚间定调入口。逐项独立容错，整体绝不抛异常。"""
    out = {"module": "market_regime", "kind": "evening", "generated_at": time.strftime("%F %T")}
    idx = {}
    for sym, label in (("sh000001", "上证指数"), ("sh000300", "沪深300")):
        try:
            idx[label] = _index_ma20(sym, label)
        except Exception as e:
            idx[label] = {"label": label, "error": type(e).__name__}
    out["indices"] = idx

    try:
        out["breadth"] = _breadth_and_amount()
    except Exception as e:
        out["breadth"] = {"error": type(e).__name__}

    try:
        zt, dt = _limit_counts()
        out["limits"] = {"zt": zt, "dt": dt}
    except Exception as e:
        out["limits"] = {"error": type(e).__name__}

    try:
        out["turnover"] = _turnover_ratio() or {"ratio": None}
    except Exception:
        out["turnover"] = {"ratio": None}

    try:
        out["classify"] = _classify_evening(idx, out.get("breadth"), 
                                            (out.get("limits", {}).get("zt"), out.get("limits", {}).get("dt")),
                                            out.get("turnover") or None)
    except Exception:
        out["classify"] = {"regime": None, "label": "数据不足不定调", "evidence": []}
    return out


def evening_push_lines(regime):
    """晚间推送头部定调段（微信文本行）。regime 为 None 或缺数据时输出降级行。"""
    if not regime:
        return ["📊 大盘定调: 数据不足，暂不定调（不编造）"]
    c = regime.get("classify") or {}
    idx = regime.get("indices") or {}
    b = regime.get("breadth") or {}
    lm = regime.get("limits") or {}
    t = regime.get("turnover") or {}
    sse, hs3 = idx.get("上证指数") or {}, idx.get("沪深300") or {}
    head = f"📊 大盘定调: 【{c.get('label', '数据不足不定调')}】"
    parts = []
    for x in (sse, hs3):
        if "bias_pct" in x:
            parts.append(f"{x.get('label', '')} {x.get('close', '数据不足')}"
                         f"（{x['bias_pct']:+.2f}% vs MA20）")
        else:
            parts.append(f"{x.get('label', '')} 数据不足")
    l2 = "   " + " | ".join(parts)
    l3 = (f"   涨/跌 {_show(b.get('adv'))}/{_show(b.get('dec'))} "
          f"| 涨停 {_show(lm.get('zt'))} 跌停 {_show(lm.get('dt'))} "
          f"| 两市成交额 {_show(b.get('amount_yi'))}亿"
          f"（环比 {_show(t.get('ratio'))}）")
    return [head, l2, l3, "   依据: " + "；".join(c.get("evidence", []) or ["无"])]


def evening_md_lines(regime):
    """晚间 ai_top10.md 顶部定调段（markdown）。"""
    if not regime:
        return ["- 📊 大盘定调: 数据不足，暂不定调（不编造）"]
    out = ["## 📊 内生趋势与情绪定调", ""]
    out.extend(evening_push_lines(regime))
    out.append("")
    out.append("> 定调为确定性规则计算，非预测模型；仅供选股参考，不构成投资建议")
    return out


# ---------------------------------------------------------------- 早间：隔夜外围与排雷
def _overnight_inputs():
    """A50 / 金龙 / 离岸人民币 三项外围数据，单项失败记 None。"""
    raw = _sina_quotes(["hf_CHA50CFD", "gb_$hxc", "fx_susdcnh"])
    def _safe(fn, key):
        try:
            return fn(raw.get(key))
        except Exception:
            return None
    return {"a50": _safe(_parse_a50, "hf_CHA50CFD"),
            "hxc": _safe(_parse_hxc, "gb_$hxc"),
            "cnh": _safe(_parse_cnh, "fx_susdcnh")}


def _overnight_news():
    """新浪全球快讯（财联社 cls 在境外挂起不可控，故用新浪，来源如实标注）。"""
    if ak is None:
        return []
    try:
        df = _with_timeout(ak.stock_info_global_sina, AK_TIMEOUT_SEC)
        if df is None or df is _TIMEOUT:
            return []
        items = []
        for _, r in df.head(NEWS_MAX).iterrows():
            txt = str(r.get("内容", "")).strip()
            items.append({"time": str(r.get("时间", "")), "text": txt[:NEWS_ITEM_CHARS]})
        return items
    except Exception:
        return []


def _deepseek_overnight_risk(news):
    """DeepSeek 宏观利空排雷 + 一句话情绪定调。只输出文本建议，不改红绿灯。
    未配置 Key / 调用失败 / 解析失败时返回降级 dict，绝不抛异常、绝不编造。"""
    base = {"risk_alert": None, "sentiment": None, "red_flag": False, "ai": None}
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        base["ai"] = "skipped(未配置OPENAI_API_KEY)"
        return base
    if not news:
        base["ai"] = "skipped(无隔夜快讯输入)"
        return base
    try:
        burl = os.environ.get("OPENAI_BASE_URL") or "https://api.deepseek.com"
        model = os.environ.get("OPENAI_MODEL") or "deepseek-chat"
        bullets = "\n".join(f"- [{x['time']}] {x['text']}" for x in news)
        prompt = (
            "你是A股开盘前风控助手。以下是隔夜至最新的宏观快讯（真实来源：新浪全球快讯）。\n"
            f"{bullets}\n\n"
            "要求：\n"
            "1. 从中挑出可能对今日A股开盘构成明显利空的宏观/政策/外部事件，做客观排雷；"
            "没有明显利空就如实说没有。\n"
            "2. 给一句话情绪定调（乐观/中性/谨慎）。\n"
            "3. 不得编造快讯中不存在的信息；不确定就写\"未见明显利空\"。\n"
            "4. 严格返回JSON: {\"risk_alert\":\"60字内\",\"sentiment\":\"40字内\",\"red_flag\":true/false}\n"
            "只返回JSON，不要其他文字。"
        )
        r = requests.post(f"{burl}/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model,
                                "messages": [{"role": "user", "content": prompt}],
                                "temperature": 0.2,
                                "response_format": {"type": "json_object"}},
                          timeout=AI_TIMEOUT)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = json.loads(text[text.find("{"):text.rfind("}") + 1])
        base.update({"risk_alert": str(obj.get("risk_alert") or "")[:120] or None,
                     "sentiment": str(obj.get("sentiment") or "")[:80] or None,
                     "red_flag": bool(obj.get("red_flag")), "ai": model})
        return base
    except Exception as e:
        base["ai"] = f"error({type(e).__name__})"
        return base


def _traffic_light(a50, hxc, cnh):
    """温控红绿灯：确定性规则。
    🔴 任一明确利空即红（防守优先）；🟢 要求三项数据齐备且全部偏多；其余🟡；全缺⚪。"""
    vals = [x for x in (a50, hxc, cnh) if x]
    if not vals:
        return "⚪", "外围数据不足，暂不定灯（不编造）"
    a = a50.get("pct") if a50 else None
    h = hxc.get("pct") if hxc else None
    c = cnh.get("pct") if cnh else None
    red = ((a is not None and a <= A50_RED) or (h is not None and h <= HXC_RED)
           or (c is not None and c >= CNH_RED))   # CNH 方向相反：美元涨=人民币贬值=利空
    green = (a is not None and a >= A50_GREEN and h is not None and h >= HXC_GREEN
             and c is not None and c <= CNH_GREEN)
    if red:
        return "🔴", "存在明确外围利空（阈值触发）"
    if green:
        return "🟢", "外围环境偏暖（三项齐备且全部达标）"
    return "🟡", "外围信号中性或数据不全（谨慎观察）"


def morning_regime(do_ai=True):
    """早间温控入口。逐项独立容错，整体绝不抛异常。"""
    out = {"module": "market_regime", "kind": "morning", "generated_at": time.strftime("%F %T")}
    try:
        out.update(_overnight_inputs())
    except Exception as e:
        out["overnight_error"] = type(e).__name__
    try:
        out["news"] = _overnight_news()
    except Exception:
        out["news"] = []
    if do_ai:
        try:
            out["ai"] = _deepseek_overnight_risk(out.get("news") or [])
        except Exception:
            out["ai"] = {"risk_alert": None, "sentiment": None, "red_flag": False,
                         "ai": "error(内部兜底)"}
    try:
        light, reason = _traffic_light(out.get("a50"), out.get("hxc"), out.get("cnh"))
        out["light"], out["light_reason"] = light, reason
    except Exception:
        out["light"], out["light_reason"] = "⚪", "数据不足"
    return out


def morning_header_text(regime):
    """早间推送头部【温控红绿灯】文本块（多行字符串；regime 为 None 时返回降级块）。"""
    if not regime:
        return "🚦 开盘温控: ⚪ 数据不足，暂不定灯（不编造）"
    a50, hxc, cnh = regime.get("a50"), regime.get("hxc"), regime.get("cnh")
    light = regime.get("light", "⚪")
    ai = regime.get("ai") or {}
    def _pct(x):
        return "数据不足" if not x or x.get("pct") is None else f"{x['pct']:+.2f}%"
    lines = [f"🚦 开盘温控: {light}（{regime.get('light_reason', '数据不足')}）",
             f"   A50期指 {_pct(a50)} | 中国金龙 {_pct(hxc)} | 离岸人民币 {_pct(cnh)}"]
    if ai.get("risk_alert"):
        lines.append(f"   AI排雷: {ai['risk_alert']}" + (" ⚠️" if ai.get("red_flag") else ""))
    if ai.get("sentiment"):
        lines.append(f"   情绪定调: {ai['sentiment']}")
    if ai.get("ai") and str(ai["ai"]).startswith(("skipped", "error")):
        lines.append(f"   （AI排雷状态: {ai['ai']}）")
    lines.append("   > 外围与排雷仅供开盘防守参考，不构成投资建议")
    return "\n".join(lines)


if __name__ == "__main__":
    # 本地自测：只读行情，不写归档、不发微信
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    if mode == "evening":
        rg = evening_regime()
        print(json.dumps(rg, ensure_ascii=False, indent=2, default=str))
        print("--- 推送头部 ---")
        print("\n".join(evening_push_lines(rg)))
        print("--- md 顶部 ---")
        print("\n".join(evening_md_lines(rg)))
    else:
        rg = morning_regime()
        print(json.dumps({k: v for k, v in rg.items()}, ensure_ascii=False, default=str))
        print("--- 推送头部 ---")
        print(morning_header_text(rg))
