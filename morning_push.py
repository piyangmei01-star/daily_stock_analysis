# -*- coding: utf-8 -*-
"""盘前选股推送脚本（轻量版）：全市场快照筛选 + 自选股竞价表现 -> 企业微信推送"""
import json
import os
import sys
import time

import requests

try:
    import efinance as ef
except ImportError:
    ef = None

WEBHOOK = os.environ.get("WECHAT_WEBHOOK_URL", "")
WATCHLIST = ["000860", "600007", "601919", "002724", "300616", "001239",
             "300314", "603273", "920418", "002626", "002613", "600736",
             "301314", "300145", "920509", "920593"]


def fetch_quotes():
    """全市场实时快照（带重试+akshare兜底）：返回 DataFrame"""
    df = None
    for i in range(4):
        try:
            df = ef.stock.get_realtime_quotes()
            if df is not None and len(df) > 1000:
                df = df.rename(columns={
                    "股票代码": "code", "股票名称": "name", "最新价": "price",
                    "涨跌幅": "pct", "换手率": "turnover", "量比": "vol_ratio",
                    "成交额": "amount", "总市值": "mktcap", "动态市盈率": "pe",
                })
                break
        except Exception as e:
            print(f"efinance 快照第{i+1}次尝试失败: {e}", flush=True)
        df = None
        time.sleep(3)
    if df is None:
        # akshare 兜底（东财同源接口，独立请求通道）
        import akshare as ak
        for i in range(3):
            try:
                df = ak.stock_zh_a_spot_em().rename(columns={
                    "代码": "code", "名称": "name", "最新价": "price",
                    "涨跌幅": "pct", "换手率": "turnover", "量比": "vol_ratio",
                    "成交额": "amount", "总市值": "mktcap", "市盈率-动态": "pe",
                })
                if df is not None and len(df) > 1000:
                    print("已切换 akshare 兜底成功", flush=True)
                    break
            except Exception as e:
                print(f"akshare 快照第{i+1}次尝试失败: {e}", flush=True)
            df = None
            time.sleep(3)
    if df is None:
        raise RuntimeError("行情快照获取失败（efinance重试4次+akshare重试3次均失败）")
    import pandas as pd
    for c in ["price", "pct", "turnover", "vol_ratio", "amount", "mktcap", "pe"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["price", "pct"])
    return df


def screen(df):
    """硬过滤 + 打分，返回 Top 候选"""
    d = df.copy()
    # 硬过滤
    d = d[~d["name"].str.contains("ST|退", na=False)]            # 剔除ST/退市
    d = d[d["code"].str.match(r"^(00|30|60)")]                    # 仅沪深主板/创业板
    d = d[d["amount"] >= 1e8]                                     # 成交额>=1亿
    d = d[d["mktcap"] >= 3e9]                                     # 市值>=30亿
    d = d[(d["price"] >= 3) & (d["price"] <= 200)]
    d = d[(d["pe"] > 0) & (d["pe"] < 120)]                        # 剔除亏损与超高估值
    d = d[d["pct"].between(1.0, 9.5)]                             # 竞价/盘中涨幅1%~9.5%
    d = d[d["vol_ratio"] >= 1.5]                                  # 量比>=1.5（放量）
    d = d[d["turnover"].between(2.0, 25.0)]                       # 换手2%~25%
    if d.empty:
        return d
    # 打分：量能+动量+热度综合
    d["score"] = (d["vol_ratio"].clip(0, 8) / 8 * 40
                  + d["turnover"].clip(0, 20) / 20 * 25
                  + (1 - (d["pct"] - 5).abs() / 5).clip(0, 1) * 20
                  + (d["amount"] / 1e9).clip(0, 3) / 3 * 15)
    return d.sort_values("score", ascending=False).head(5)


def watch_performance(df):
    """自选股竞价/盘中表现"""
    rows = []
    for code in WATCHLIST:
        r = df[df["code"] == code]
        if r.empty:
            rows.append(f"⚪ {code} 暂无数据")
            continue
        r = r.iloc[0]
        emoji = "🔴" if r["pct"] > 0 else ("🟢" if r["pct"] < 0 else "⚪")
        rows.append(f"{emoji} **{r['name']}**({code}): {r['pct']:+.2f}% "
                    f"量比{r['vol_ratio']:.1f} 换手{r['turnover']:.1f}%")
    return rows


def push(content):
    """发送纯文本（微信插件可读），超长自动分条"""
    plain = content.replace("**", "")
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
        print(r.text)
        try:
            ok = ok and r.json().get("errcode") == 0
        except Exception:
            ok = False
    return ok


def main():
    if ef is None:
        print("efinance 未安装")
        sys.exit(1)
    df = fetch_quotes()
    picks = screen(df)
    lines = [f"☀️ **盘前选股清单** {time.strftime('%m-%d %H:%M')}\n"]
    if picks.empty:
        lines.append("今日无符合条件的候选（市场过冷或条件过严），建议观望。")
    else:
        lines.append("**🔍 今日候选 Top 5**（量能+动量+热度综合评分）\n")
        for _, r in picks.iterrows():
            lines.append(f"{len(lines)}. **{r['name']}**({r['code']}) {r['price']:.2f} "
                         f"{r['pct']:+.2f}% | 量比{r['vol_ratio']:.1f} 换手{r['turnover']:.1f}% "
                         f"成交{r['amount']/1e8:.1f}亿 | 评分{r['score']:.0f}")
    lines.append("\n**📋 你的自选股表现**\n")
    lines.extend(watch_performance(df))
    lines.append("\n> ⚠️ 量化筛选仅供参考，不构成投资建议")
    ok = push("\n".join(lines))
    print("PUSH_OK" if ok else "PUSH_FAIL")


if __name__ == "__main__":
    main()
