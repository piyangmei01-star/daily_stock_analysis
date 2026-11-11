# -*- coding: utf-8 -*-
"""V2 统一行情数据层（Stage 1 修复项，独立新增文件，不改 V1 算法）

职责
----
1. 多数据源实时全市场快照：eastmoney_clist（主） -> sina_market_center（备）
2. 统一口径，任何来源返回完全一致的表结构：
   - 股票代码：6 位字符串，去掉 sh/sz/bj 市场前缀，左侧补零
   - 交易日期：data_date = YYYY-MM-DD（北京时间），来自接口行情时间戳 f124，
     无法取得时由交易日历推断并标注 date_source=calendar_inferred
   - 价格口径：快照为「未复权」实时/收盘价（price_basis=unadjusted）；
     K 线由 V1 trend_ok 使用「前复权 fqt=1」，两者绝不混用
   - 金额单位：元（CNY）；新浪 mktcap 原始单位为万元，统一乘 1e4
   - 字段：code,name,price,pct,turnover,vol_ratio,amount,mktcap,
           open,high,low,prev_close,source
3. 全市场完整性硬校验：实际条数必须 >= 5000 且 >= 接口 total*0.95，
   否则抛 MarketDataError。绝不把 200 条当成全市场，绝不静默降级。
4. 缓存按「数据日期 + 行情基准」命名，读取时严格校验，
   跨日/跨基准缓存一律拒绝，绝不把历史缓存冒充当日行情。
5. 任何失败写 run_logs（ERROR/WARN），并向上抛出明确异常。

本模块只负责「行情快照」，不产生选股结论、不推送、不训练模型。
"""
import datetime as dt
import json
import math
import os
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import history_store as hs

TZ = ZoneInfo("Asia/Shanghai")

MIN_UNIVERSE = 5000          # 全市场最少股票数硬门槛
COMPLETENESS_RATIO = 0.95    # 相对接口 total 的最低完整率
CACHE_DIR = "data/cache"

FIELDS = ["code", "name", "price", "pct", "turnover", "vol_ratio",
          "amount", "mktcap", "open", "high", "low", "prev_close", "pe", "source"]

EM_HOSTS = ["82.push2.eastmoney.com", "push2.eastmoney.com",
            "82.push2delay.eastmoney.com", "33.push2.eastmoney.com",
            "push2delay.eastmoney.com", "1.push2.eastmoney.com",
            "7.push2.eastmoney.com", "90.push2.eastmoney.com"]
EM_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"   # 沪深主板+创业板+科创板
EM_FIELDS = "f12,f14,f2,f3,f8,f10,f6,f20,f9,f124"

EM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
              "Referer": "https://quote.eastmoney.com/"}
SINA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://finance.sina.com.cn/"}
SINA_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
            "json_v2.php/Market_Center.getHQNodeData")
SINA_COUNT_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                  "json_v2.php/Market_Center.getHQNodeStockCount")


class MarketDataError(RuntimeError):
    """行情数据不可用（不完整 / 日期不符 / 全部数据源失败），必须显式报错"""


def _log(level, msg):
    try:
        hs.log_event("market_data", level, msg)
    except Exception:
        pass
    print(f"[market_data][{level}] {msg}", flush=True)


def now_bj():
    return dt.datetime.now(TZ)


# ---------------------------------------------------------------- 交易日历
def _trade_dates():
    """交易日历（YYYY-MM-DD 列表）。优先本地缓存，其次 akshare，最后周一至周五降级。"""
    path = "data/trade_calendar.json"
    if os.path.exists(path):
        try:
            with open(path) as fh:
                d = json.load(fh)
            if isinstance(d, list) and len(d) > 100:
                return sorted(str(x)[:10] for x in d)
            if isinstance(d, dict) and len(d.get("dates", [])) > 100:
                return sorted(str(x)[:10] for x in d["dates"])
        except Exception:
            pass
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        dates = [str(x)[:10] for x in cal.iloc[:, 0].tolist()]
        os.makedirs("data", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(dates, fh)
        _log("INFO", f"交易日历已从 akshare 拉取: {len(dates)} 天")
        return sorted(dates)
    except Exception as e:
        _log("WARN", f"交易日历获取失败，降级为周一至周五: {e}")
        d0 = dt.date(2024, 1, 1)
        return [(d0 + dt.timedelta(days=i)).isoformat()
                for i in range(1500) if (d0 + dt.timedelta(days=i)).weekday() < 5]


def expected_data_date():
    """返回 (期望数据日期, 行情基准)。

    - 交易日 09:30 之后：期望=当日，基准=intraday（真实盘中/收盘行情）
    - 交易日 09:30 之前：期望=上一交易日，基准=prev_close（盘前只能用昨收）
    - 非交易日：期望=最近已收盘交易日，基准=close
    """
    now = now_bj()
    today = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")
    cal = set(_trade_dates())
    if today in cal and hm >= "09:30":
        return today, "intraday"
    if today in cal:
        prev = max((x for x in cal if x < today), default=today)
        return prev, "prev_close"
    last = max((x for x in cal if x <= today), default=today)
    return last, "close"


# ---------------------------------------------------------------- 通用工具
def _norm_code(x):
    s = str(x).strip().lower()
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            s = s[2:]
            break
    s = "".join(ch for ch in s if ch.isdigit())
    return s.zfill(6)[-6:]


def _num(v, scale=1.0):
    try:
        if v is None or v == "" or v == "-":
            return None
        return float(v) * scale
    except (TypeError, ValueError):
        return None


def _empty_df():
    return pd.DataFrame(columns=FIELDS)


def _finalize(rows, source, data_date, date_source, quote_basis, total):
    """统一字段、去重（同代码保留首条）、构造元数据"""
    df = pd.DataFrame(rows)
    for c in FIELDS:
        if c not in df.columns:
            df[c] = None
    df = df[FIELDS]
    df["code"] = df["code"].map(_norm_code)
    df = df[df["code"].str.match(r"^\d{6}$", na=False)]
    df = df.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
    for c in ["price", "pct", "turnover", "vol_ratio", "amount", "mktcap",
              "open", "high", "low", "prev_close", "pe"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    meta = {
        "source": source,
        "data_date": data_date,
        "date_source": date_source,
        "quote_basis": quote_basis,
        "universe_total": int(total or 0),
        "rows": int(len(df)),
        "complete": False,
        "fetched_at": now_bj().strftime("%Y-%m-%d %H:%M:%S"),
        "price_basis": "unadjusted",
        "kline_basis": "forward_adjusted_fqt1 (V1 trend_ok 使用，不与快照混用)",
        "code_rule": "6位数字字符串，去市场前缀",
        "amount_unit": "CNY", "mktcap_unit": "CNY",
    }
    return df, meta


def verify(df, meta, expected=None):
    """全市场完整性 + 数据日期硬校验。不通过即抛错，绝不返回残缺数据。"""
    expected = expected or expected_data_date()
    exp_date, exp_basis = expected
    need = max(MIN_UNIVERSE, int(meta["universe_total"] * COMPLETENESS_RATIO))
    if len(df) < need:
        raise MarketDataError(
            f"全市场数据不完整: 来源={meta['source']} 实得={len(df)} 只, "
            f"接口total={meta['universe_total']}, 门槛={need} 只。"
            f"拒绝使用残缺快照（绝不把 {len(df)} 只冒充全市场）")
    if meta["data_date"] != exp_date:
        raise MarketDataError(
            f"行情日期不符: 来源={meta['source']} data_date={meta['data_date']} "
            f"!= 期望 {exp_date}({exp_basis})。拒绝把非当日行情当作当日数据")
    if meta["quote_basis"] != exp_basis:
        raise MarketDataError(
            f"行情基准不符: 来源={meta['source']} quote_basis={meta['quote_basis']} "
            f"!= 期望 {exp_basis}。拒绝把历史/盘前缓存冒充当前时段行情")
    meta["complete"] = True
    return True


# ---------------------------------------------------------------- 数据源 1：东财
def fetch_eastmoney():
    """东财 clist 分页拉全市场。任一页失败即报错，不静默截断。"""
    sess = requests.Session()
    sess.headers.update(EM_HEADERS)
    rows, page, total = [], 1, None
    while page <= 40:
        params = {"pn": page, "pz": 200, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                  "fid": "f12", "fs": EM_FS, "fields": EM_FIELDS}
        got = None
        last_err = ""
        for attempt in range(6):
            host = EM_HOSTS[(page - 1 + attempt) % len(EM_HOSTS)]
            try:
                r = sess.get(f"https://{host}/api/qt/clist/get", params=params, timeout=15)
                j = r.json().get("data")
                if j and j.get("diff") is not None:
                    got = j
                    break
                last_err = f"HTTP{r.status_code} data为空"
            except Exception as e:
                last_err = f"{type(e).__name__}"
            time.sleep(min(1 + attempt, 4))
        if got is None:
            raise MarketDataError(f"东财第{page}页6次重试全部失败({last_err})，已获取{len(rows)}只，放弃本次快照")
        diff = got["diff"]
        if isinstance(diff, dict):
            diff = list(diff.values())
        rows.extend(diff)
        total = got.get("total") or total
        if (total and len(rows) >= total) or not diff:
            break
        page += 1
    if not rows:
        raise MarketDataError("东财返回 0 条行情")

    # f124 = 行情时间戳（Unix 秒）-> 北京时间数据日期
    ts = rows[0].get("f124")
    if ts:
        try:
            d = dt.datetime.fromtimestamp(int(ts), TZ)
            data_date = d.strftime("%Y-%m-%d")
            date_source = "f124_timestamp"
            if d.strftime("%H:%M") >= "15:00":
                qb = "close"
            elif d.strftime("%H:%M") >= "09:30":
                qb = "intraday"
            else:
                qb = "call_auction"
        except Exception:
            data_date, date_source, qb = expected_data_date()[0], "calendar_inferred", expected_data_date()[1]
    else:
        data_date, date_source, qb = expected_data_date()[0], "calendar_inferred", expected_data_date()[1]

    out = []
    for x in rows:
        out.append({"code": x.get("f12"), "name": x.get("f14"),
                    "price": _num(x.get("f2")), "pct": _num(x.get("f3")),
                    "turnover": _num(x.get("f8")), "vol_ratio": _num(x.get("f10")),
                    "amount": _num(x.get("f6")), "mktcap": _num(x.get("f20")),
                    "open": None, "high": None, "low": None, "prev_close": None,
                    "pe": _num(x.get("f9")), "source": "eastmoney_clist"})
    return _finalize(out, "eastmoney_clist", data_date, date_source, qb, total)


# ---------------------------------------------------------------- 数据源 2：新浪
def fetch_sina():
    """新浪 Market_Center 全市场（独立数据源，不依赖东财）。
    注意：num 参数被服务端固定为 100，必须按 page 分页；返回 GBK JSON。
    """
    sess = requests.Session()
    sess.headers.update(SINA_HEADERS)
    try:
        r = sess.get(SINA_COUNT_URL, params={"node": "hs_a"}, timeout=15)
        total = int(str(r.text).strip().strip('"') or 0)
    except Exception as e:
        raise MarketDataError(f"新浪股票总数接口失败: {type(e).__name__}")
    if total < MIN_UNIVERSE:
        raise MarketDataError(f"新浪股票总数异常: {total}")
    pages = math.ceil(total / 100)
    rows = []
    for page in range(1, pages + 1):
        data = None
        last_err = ""
        for attempt in range(3):
            try:
                r = sess.get(SINA_URL, params={"page": page, "num": 100, "sort": "symbol",
                                              "asc": 1, "node": "hs_a"}, timeout=20)
                r.encoding = "gbk"
                data = json.loads(r.text)
                break
            except Exception as e:
                last_err = type(e).__name__
                time.sleep(1 + attempt)
        if data is None:
            raise MarketDataError(f"新浪第{page}/{pages}页3次重试失败({last_err})，已获取{len(rows)}只，放弃本次快照")
        if not data:
            break
        rows.extend(data)
        if len(rows) >= total:
            break

    exp_date, exp_basis = expected_data_date()
    out = []
    for x in rows:
        sym = str(x.get("symbol", "")).lower()
        if sym.startswith("bj"):        # 北交所不在本项目股票范围（与 V1 universe 一致）
            continue
        code = _norm_code(x.get("code") or sym)
        # 范围：沪市主板60 / 科创板688 / 深市主板00 / 创业板30
        if not code.startswith(("00", "30", "60", "68")):
            continue
        out.append({"code": code, "name": x.get("name"),
                    "price": _num(x.get("trade")), "pct": _num(x.get("changepercent")),
                    "turnover": _num(x.get("turnoverratio")), "vol_ratio": None,
                    "amount": _num(x.get("amount")),
                    "mktcap": _num(x.get("mktcap"), 1e4),   # 万元 -> 元
                    "open": _num(x.get("open")), "high": _num(x.get("high")),
                    "low": _num(x.get("low")), "prev_close": _num(x.get("settlement")),
                    "pe": _num(x.get("per")), "source": "sina_market_center"})
    # 新浪全市场源不提供量比(vol_ratio)，统一置空，由 enrich_vol_ratio_from_tencent 补齐；
    # 补齐前 V1 强势股筛选会因量比为空而返回空集，调用方必须显式处理并记日志。
    # 新浪无行情日期字段，明确标注为日历推断。
    # total 含北交所，不属于本项目股票范围；完整性门槛必须基于过滤后的有效域，
    # 否则北交所数量变化会造成误判。原始总数单独留痕。
    df, meta = _finalize(out, "sina_market_center", exp_date,
                         "calendar_inferred", exp_basis, len(out))
    meta["raw_total_including_bj"] = total
    meta["excluded_beijing"] = len(rows) - len(out)
    meta["universe_scope"] = "沪深主板00/60 + 创业板30 + 科创板68（排除北交所）"
    return df, meta


# ---------------------------------------------------------------- 腾讯（量比补齐 / 单股备用）
def _tx_symbol(code):
    c = _norm_code(code)
    return ("sh" if c.startswith(("6", "9")) else "sz") + c


def fetch_tencent_batch(codes, batch=50):
    """腾讯批量实时行情。字段：1名称 2代码 3现价 4昨收 30时间戳 32涨跌幅
    38换手率 39市盈率 49量比。时间戳自带数据日期，可独立校验。"""
    out = {}
    codes = list(dict.fromkeys(_norm_code(c) for c in codes))
    sess = requests.Session()
    sess.headers.update({"User-Agent": EM_HEADERS["User-Agent"],
                         "Referer": "https://gu.qq.com/"})
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        q = ",".join(_tx_symbol(c) for c in chunk)
        try:
            r = sess.get("https://qt.gtimg.cn/q=" + q, timeout=15)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                if "~" not in line:
                    continue
                p = line.split("~")
                if len(p) < 50:
                    continue
                c = _norm_code(p[2])
                ts = p[30]
                out[c] = {"name": p[1], "price": _num(p[3]),
                          "prev_close": _num(p[4]), "pct": _num(p[32]),
                          "turnover": _num(p[38]), "vol_ratio": _num(p[49]),
                          "pe": _num(p[39]),
                          "data_date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else None,
                          "source": "tencent_qt"}
        except Exception as e:
            _log("WARN", f"腾讯批量行情第{i // batch + 1}批失败: {type(e).__name__}: {e}")
    return out


def enrich_vol_ratio_from_tencent(df, codes=None):
    """新浪备用源缺少量比/PE 时，用腾讯批量补齐（只补指定候选，控制请求量）。
    返回 (df, 成功补齐只数, 来源)。补齐失败不静默，由调用方判断。"""
    if codes is None:
        codes = df["code"].tolist()
    codes = list(dict.fromkeys(_norm_code(c) for c in codes))
    if not codes:
        return df, 0, "tencent_qt"
    q = fetch_tencent_batch(codes)
    n = 0
    for c, v in q.items():
        mask = df["code"] == c
        if not mask.any():
            continue
        for col in ["vol_ratio", "turnover", "pe", "pct", "price"]:
            if v.get(col) is not None and df.loc[mask, col].isna().all():
                df.loc[mask, col] = v[col]
        n += 1
    _log("INFO", f"腾讯补量比: 请求{len(codes)}只, 成功{n}只 (source=tencent_qt)")
    return df, n, "tencent_qt"


def fetch_stock_quote(code):
    """单股多源备用：东财 -> 腾讯 -> 新浪，返回统一 dict；全失败返回 None。"""
    c = _norm_code(code)
    secid = ("1." if c.startswith("6") else "0.") + c
    try:
        r = requests.get("https://push2.eastmoney.com/api/qt/stock/get",
                         params={"secid": secid, "fltt": 2, "invt": 2,
                                 "fields": "f58,f43,f170,f168,f50,f60"},
                         headers=EM_HEADERS, timeout=10)
        d = r.json().get("data") or {}
        if d and d.get("f43") not in (None, "-"):
            return {"code": c, "name": d.get("f58"), "price": d.get("f43"),
                    "pct": d.get("f170"), "turnover": d.get("f168"),
                    "vol_ratio": d.get("f50"), "prev_close": d.get("f60"),
                    "source": "eastmoney_stock_get",
                    "data_date": expected_data_date()[0]}
    except Exception:
        pass
    q = fetch_tencent_batch([c])
    if c in q:
        v = q[c]
        return {"code": c, "name": v["name"], "price": v["price"], "pct": v["pct"],
                "turnover": v["turnover"], "vol_ratio": v["vol_ratio"],
                "prev_close": v["prev_close"], "source": "tencent_qt",
                "data_date": v["data_date"]}
    try:
        r = requests.get("https://hq.sinajs.cn/list=" + _tx_symbol(c),
                         headers=SINA_HEADERS, timeout=10)
        r.encoding = "gbk"
        s = r.text.split('"')[1].split(",")
        if len(s) > 8 and _num(s[3]):
            prev = _num(s[2])
            return {"code": c, "name": s[0], "price": _num(s[3]),
                    "pct": round((_num(s[3]) / prev - 1) * 100, 2) if prev else None,
                    "turnover": None, "vol_ratio": None, "prev_close": prev,
                    "source": "sina_hq", "data_date": expected_data_date()[0]}
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 缓存
def _cache_paths(data_date, quote_basis):
    stem = f"snapshot_{data_date.replace('-', '')}_{quote_basis}"
    return os.path.join(CACHE_DIR, stem + ".csv"), os.path.join(CACHE_DIR, stem + ".meta.json")


def save_cache(df, meta):
    os.makedirs(CACHE_DIR, exist_ok=True)
    meta = dict(meta)
    meta["schema_version"] = 2
    meta["schema_fields"] = list(FIELDS)
    cp, mp = _cache_paths(meta["data_date"], meta["quote_basis"])
    df.to_csv(cp, index=False)
    with open(mp, "w") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    _log("INFO", f"快照已缓存: {cp} ({len(df)}只, source={meta['source']}, "
                 f"data_date={meta['data_date']}, basis={meta['quote_basis']})")


def load_cache(expected=None):
    """严格校验后才允许复用缓存：数据日期、行情基准、完整性三项全匹配。"""
    expected = expected or expected_data_date()
    exp_date, exp_basis = expected
    cp, mp = _cache_paths(exp_date, exp_basis)
    if not (os.path.exists(cp) and os.path.exists(mp)):
        return None
    try:
        with open(mp) as fh:
            meta = json.load(fh)
        if meta.get("data_date") != exp_date or meta.get("quote_basis") != exp_basis:
            _log("WARN", f"缓存日期/基准不匹配，拒绝复用: {mp}")
            return None
        if not meta.get("complete"):
            return None
        if meta.get("schema_version") != 2 or meta.get("schema_fields") != list(FIELDS):
            _log("WARN", f"缓存字段结构与当前版本不一致，拒绝复用: {mp}")
            return None
        df = pd.read_csv(cp, dtype={"code": str})
        missing = [c for c in FIELDS if c not in df.columns]
        if missing:
            _log("WARN", f"缓存缺少字段 {missing}，拒绝复用: {cp}")
            return None
        df = df[FIELDS]
        df["code"] = df["code"].map(_norm_code)
        verify(df, meta, expected)
        _log("INFO", f"复用当日缓存 source={meta['source']} data_date={exp_date} "
                     f"basis={exp_basis} rows={len(df)}（同一数据日期，非历史冒充）")
        return df, meta
    except MarketDataError:
        raise
    except Exception as e:
        _log("WARN", f"缓存读取失败，转为实时拉取: {e}")
        return None


# ---------------------------------------------------------------- 主入口
def fetch_snapshot(use_cache=True, sources=None, expected=None):
    """获取全市场快照。

    返回 (df, meta)。meta 明确标注来源、实际数据日期、行情基准、完整性与口径。
    失败抛 MarketDataError（已写 ERROR 日志），调用方必须显式处理，不得静默继续。
    """
    expected = expected or expected_data_date()
    if use_cache:
        cached = load_cache(expected)
        if cached:
            return cached

    sources = sources or [fetch_eastmoney, fetch_sina]
    errors = []
    for fn in sources:
        name = fn.__name__
        try:
            df, meta = fn()
            verify(df, meta, expected)
            save_cache(df, meta)
            _log("INFO", f"全市场快照成功: source={meta['source']} rows={len(df)} "
                         f"total={meta['universe_total']} data_date={meta['data_date']} "
                         f"basis={meta['quote_basis']} date_source={meta['date_source']}")
            return df, meta
        except MarketDataError as e:
            errors.append(f"{name}: {e}")
            _log("WARN", f"{name} 不可用 -> {e}")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            _log("WARN", f"{name} 异常 -> {type(e).__name__}: {e}")

    msg = "全部行情数据源失败，本次选股终止（不使用残缺/历史数据）: " + " | ".join(errors)
    _log("ERROR", msg)
    raise MarketDataError(msg)


if __name__ == "__main__":
    import sys
    try:
        df, meta = fetch_snapshot(use_cache="--no-cache" not in sys.argv)
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        print(df.head(3).to_string())
    except MarketDataError as e:
        print("MARKET_DATA_ERROR:", e)
        sys.exit(1)
