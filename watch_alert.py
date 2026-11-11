# -*- coding: utf-8 -*-
"""自选股异动监控：大涨大跌/放量/临停预警，触发才推送企业微信"""
import os
import sys
import time

import efinance as ef
import pandas as pd
import requests

WEBHOOK = os.environ.get("WECHAT_WEBHOOK_URL", "")
WATCHLIST = [c.strip() for c in os.environ.get(
    "STOCK_LIST", "000860,600007,601919,002724,300616,001239,300314,603273,920418,002626,002613,600736,301314,300145,920509,920593"
).split(",") if c.strip()]


def in_trading_time():
    """仅交易时段推送（9:30-11:30, 13:00-15:00），测试模式用 --force 跳过"""
    if "--force" in sys.argv:
        return True
    now = time.localtime()
    hm = now.tm_hour * 100 + now.tm_min
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def check():
    df = None
    for i in range(4):
        try:
            df = ef.stock.get_realtime_quotes()
            if df is not None and len(df) > 1000:
                df = df.rename(columns={
                    "股票代码": "code", "股票名称": "name", "最新价": "price", "涨跌幅": "pct",
                    "换手率": "turnover", "量比": "vol_ratio", "成交额": "amount"})
                break
        except Exception as e:
            print(f"efinance 快照第{i+1}次尝试失败: {e}", flush=True)
        df = None
        time.sleep(3)
    if df is None:
        import akshare as ak
        for i in range(3):
            try:
                df = ak.stock_zh_a_spot_em().rename(columns={
                    "代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "pct",
                    "换手率": "turnover", "量比": "vol_ratio", "成交额": "amount"})
                if df is not None and len(df) > 1000:
                    print("已切换 akshare 兜底成功", flush=True)
                    break
            except Exception as e:
                print(f"akshare 快照第{i+1}次尝试失败: {e}", flush=True)
            df = None
            time.sleep(3)
    if df is None:
        print("PUSH_FAIL")
        return
    for c in ["price", "pct", "turnover", "vol_ratio"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    alerts = []
    for code in WATCHLIST:
        r = df[df["code"] == code]
        if r.empty:
            continue
        r = r.iloc[0]
        tags = []
        if r["pct"] >= 9.5:
            tags.append("🚀 涨停/临停")
        elif r["pct"] >= 5:
            tags.append("🔥 大涨")
        if r["pct"] <= -9.5:
            tags.append("💥 跌停/临停")
        elif r["pct"] <= -5:
            tags.append("🌊 大跌")
        if r["vol_ratio"] >= 3:
            tags.append(f"📣 放量(量比{r['vol_ratio']:.1f})")
        if tags:
            e = "🔴" if r["pct"] > 0 else "🟢"
            alerts.append(f"{e} **{r['name']}**({code}) {r['price']:.2f} {r['pct']:+.2f}% 换手{r['turnover']:.1f}%\n" + " ".join(tags))
    return alerts


def push(text):
    plain = text.replace("**", "")
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
        try:
            ok = ok and r.json().get("errcode") == 0
        except Exception:
            ok = False
    return ok


def main():
    if not in_trading_time():
        print("SKIP_NON_TRADING")
        return
    alerts = check()
    if not alerts:
        print("NO_ALERT")
        return
    content = f"⚡ **自选股异动警报** {time.strftime('%H:%M')}\n\n" + "\n\n".join(alerts) + "\n\n> ⚠️ 量价异动提醒，注意风险"
    print("PUSH_OK" if push(content) else "PUSH_FAIL")


if __name__ == "__main__":
    main()
