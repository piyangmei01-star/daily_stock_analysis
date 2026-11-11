# -*- coding: utf-8 -*-
"""质量选股推送：基本面质量 + 趋势健康 + 流动性 三重把关 -> 企业微信"""
import concurrent.futures as cf
import os
import sys
import time

import akshare as ak
import efinance as ef
import pandas as pd
import requests

WEBHOOK = os.environ.get("WECHAT_WEBHOOK_URL", "")
WATCHLIST = ["000860", "600007", "601919", "002724", "300616", "001239",
             "300314", "603273", "920418", "002626", "002613", "600736",
             "301314", "300145", "920509", "920593"]
REPORT_DATE = "20260930"  # 最新财报期


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_industry_map(codes=None):
    """行业分类映射（东财直连接口，缓存到 data/industry_map.json，只查缺失代码）"""
    import json
    import os
    path = "data/industry_map.json"
    m = {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                m = json.load(fh)
        except Exception:
            m = {}
    if not codes:
        return m
    todo = [c for c in codes if c not in m]
    for c in todo:
        secid = ("1." if c.startswith("6") else "0.") + c
        for _ in range(3):
            try:
                r = requests.get("https://push2.eastmoney.com/api/qt/stock/get",
                                 params={"secid": secid, "fields": "f127"}, timeout=10,
                                 headers={"User-Agent": "Mozilla/5.0"})
                ind = (r.json().get("data") or {}).get("f127")
                if ind:
                    m[c] = ind
                break
            except Exception:
                time.sleep(2)
    if todo:
        os.makedirs("data", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(m, fh, ensure_ascii=False)
        log(f"行业映射: 本次查询 {len(todo)} 只，缓存共 {len(m)} 只")
    return m


def fetch_quotes_df():
    """全市场快照：efinance 重试3次，失败则用 akshare 兜底"""
    last = None
    for i in range(3):
        try:
            df = ef.stock.get_realtime_quotes()
            if df is not None and len(df) > 1000:
                return df.rename(columns={
                    "股票代码": "code", "股票名称": "name", "最新价": "price", "涨跌幅": "pct",
                    "换手率": "turnover", "量比": "vol_ratio", "成交额": "amount", "总市值": "mktcap"})
        except Exception as e:
            last = e
        log(f"efinance 快照第{i+1}次尝试失败，重试...")
        time.sleep(3)
    log("切换 akshare 兜底数据源...")
    df = ak.stock_zh_a_spot_em().rename(columns={
        "代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "pct",
        "换手率": "换手率", "量比": "vol_ratio", "成交额": "amount", "总市值": "mktcap"})
    df["turnover"] = df["换手率"]
    return df


def load_snapshot():
    """第一关：行情快照硬过滤"""
    df = fetch_quotes_df()
    import numpy as np
    for c in ["price", "pct", "turnover", "vol_ratio", "amount", "mktcap"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["price", "pct"])
    d = df[~df["name"].str.contains("ST|退", na=False)]
    d = d[d["code"].str.match(r"^(00|30|60)")]           # 沪深主板+创业板
    d = d[(d["price"] >= 3) & (d["price"] <= 200)]
    d = d[d["mktcap"] >= 5e9]                            # 市值>=50亿
    d = d[d["amount"] >= 1.5e8]                          # 成交额>=1.5亿
    d = d[d["pct"].between(-2, 7)]                       # 不追暴涨暴跌
    log(f"快照过滤后: {len(d)} 只")
    return d


def load_fundamentals(snap):
    """第二关：业绩质量（净利润/营收正增长 + ROE + EPS）"""
    yj = ak.stock_yjbb_em(date=REPORT_DATE).rename(columns={
        "股票代码": "code", "净利润-同比增长": "profit_g", "营业总收入-同比增长": "rev_g",
        "净资产收益率": "roe", "每股收益": "eps"})
    for c in ["profit_g", "rev_g", "roe", "eps"]:
        yj[c] = pd.to_numeric(yj[c], errors="coerce")
    m = snap.merge(yj[["code", "profit_g", "rev_g", "roe", "eps"]], on="code", how="inner")
    m = m[(m["profit_g"] > 0) & (m["rev_g"] > 0)         # 利润与营收双增
          & (m["roe"] > 2) & (m["eps"] > 0)]              # ROE>2%、不亏损
    m["fund_score"] = (m["profit_g"].clip(0, 50) / 50 * 40   # 利润增速(封顶50%)
                       + m["rev_g"].clip(0, 30) / 30 * 25    # 营收增速(封顶30%)
                       + m["roe"].clip(0, 20) / 20 * 25      # ROE(封顶20%)
                       + (m["amount"] / 1e9).clip(0, 2) / 2 * 10)  # 流动性
    m = m.sort_values("fund_score", ascending=False).head(500)  # 限制后续K线拉取量
    log(f"业绩质量过滤后: {len(m)} 只")
    return m


def trend_ok(code):
    """第三关：趋势健康（站上MA20/MA60、多头、未严重超买）"""
    try:
        k = ef.stock.get_quote_history(code)
        if len(k) < 70:
            return None
        close = k["收盘"].astype(float)
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]
        c = close.iloc[-1]
        chg20 = (c / close.iloc[-21] - 1) * 100
        if c > ma20 > ma60 and 0 <= chg20 <= 15:  # 多头排列 + 收紧追高上限15%
            return code, ma20, ma60, chg20
    except Exception:
        pass
    return None


def main():
    snap = load_snapshot()
    fund = load_fundamentals(snap)
    codes = fund["code"].tolist()
    log(f"开始拉取 {len(codes)} 只候选K线判断趋势...")
    passed = {}
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(trend_ok, codes):
            if res:
                passed[res[0]] = res[1:]
    log(f"趋势过滤后: {len(passed)} 只")
    if not passed:
        push("今日无同时满足「业绩质量+趋势多头」的候选，建议观望。")
        return
    final = fund[fund["code"].isin(passed.keys())].copy()
    final["trend20"] = final["code"].map(lambda c: passed[c][2])
    final["total"] = final["fund_score"] * 0.65 + (30 - final["trend20"].clip(0, 30)) / 30 * 35
    top = final.sort_values("total", ascending=False).head(10)

    # 行业分散：同行业最多保留2只（只查 Top10 的行业，速度快）
    industry = get_industry_map(top["code"].tolist())
    picked, cnt = [], {}
    for _, r in top.iterrows():
        ind = industry.get(r["code"], "其他")
        if cnt.get(ind, 0) >= 2:
            continue
        cnt[ind] = cnt.get(ind, 0) + 1
        picked.append(r)
        if len(picked) >= 10:
            break
    top = pd.DataFrame(picked)

    # 保存最终名单（供 AI 深度分析任务使用）
    codes = top["code"].tolist()
    with open("data/screened_top10.txt", "w") as f:
        f.write(",".join(codes))
    log(f"Top10 已保存: {','.join(codes)}")

    if "--save-only" in sys.argv:
        print("SAVE_OK")
        return

    lines = [f"💎 **质量选股 Top{len(top)}** {time.strftime('%m-%d %H:%M')}\n",
             "三重把关：业绩双增+ROE | 多头趋势(追高≤15%) | 行业分散\n"]
    for i, (_, r) in enumerate(top.iterrows(), 1):
        ind = industry.get(r["code"], "")
        lines.append(f"{i}. **{r['name']}**({r['code']}) {r['price']:.2f} {r['pct']:+.2f}% [{ind}]")
        lines.append(f"   净利+{r['profit_g']:.0f}% 营收+{r['rev_g']:.0f}% ROE {r['roe']:.1f}% "
                     f"| 20日涨{r['trend20']:.0f}% 市值{r['mktcap']/1e8:.0f}亿")
    wl = snap[snap["code"].isin(WATCHLIST)]
    if not wl.empty:
        lines.append("\n**📋 自选股表现**")
        for _, r in wl.iterrows():
            e = "🔴" if r["pct"] > 0 else "🟢"
            lines.append(f"{e} {r['name']} {r['pct']:+.2f}%")
    lines.append("\n> ⚠️ 质量筛选仅供参考，不构成投资建议")
    push("\n".join(lines))


def push(content):
    """发送纯文本（微信插件可读），超长自动分条"""
    import re
    plain = content.replace("**", "").replace("# ", "")
    plain = "\n".join(l for l in plain.split("\n") if not l.strip().startswith(">"))
    # text 类型上限 2048 字节，按行分块
    chunks, cur, size = [], [], 0
    for line in plain.split("\n"):
        b = len(line.encode("utf-8")) + 1
        if size + b > 1900 and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += b
    if cur:
        chunks.append("\n".join(cur))
    ok = True
    for c in chunks:
        r = requests.post(WEBHOOK, json={"msgtype": "text", "text": {"content": c}}, timeout=15)
        print(r.text, flush=True)
        try:
            ok = ok and r.json().get("errcode") == 0
        except Exception:
            ok = False
    return ok


if __name__ == "__main__":
    main()
