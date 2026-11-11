# -*- coding: utf-8 -*-
"""V2 质量选股：算法完全复用 V1（import quality_screen），新增历史归档能力。

- 全市场候选（业绩过滤后）+ 趋势通过候选 + Top10 全部落库并按日归档
- 保存代码/名称/日期/价格/评分/因子分/入选理由/风险标签/行业/市场环境/数据时间
- 同日重复运行：UNIQUE 去重，重复记录=0；历史日期永不覆盖
- --no-push 仅归档不推送（测试用）；--save-only 兼容 V1
"""
import os
import sys

import pandas as pd
import requests

import history_store as hs
import quality_screen as v1

MODEL_VERSION = "v2.0-stage1"
TASK = "quality"


def log(msg):
    print(msg, flush=True)


def market_env():
    """市场环境：指数实时涨跌（东财直连）"""
    env = {"captured_at": hs._ts()}
    for secid, key in [("1.000300", "hs300"), ("1.000001", "sh_index")]:
        try:
            r = requests.get("https://push2.eastmoney.com/api/qt/stock/get",
                             params={"secid": secid, "fields": "f43,f170", "fltt": 2, "invt": 2},
                             headers={"User-Agent": "Mozilla/5.0",
                                      "Referer": "https://quote.eastmoney.com/"}, timeout=10)
            d = r.json().get("data") or {}
            env[key + "_price"] = d.get("f43")
            env[key + "_pct"] = d.get("f170")
        except Exception as e:
            hs.log_event(TASK, "WARN", f"市场环境指数获取失败 {secid}: {e}")
    return env


def risk_tags(r):
    tags = []
    if r.get("trend20", 0) > 10:
        tags.append("20日涨幅偏高(>10%)")
    if r.get("pct", 0) > 5:
        tags.append("当日涨幅>5%")
    if r.get("turnover", 0) and r["turnover"] > 15:
        tags.append("高换手(>15%)")
    if r.get("mktcap", 1e11) < 1e10:
        tags.append("市值偏小(<100亿)")
    return ";".join(tags) if tags else "无显著风险标签"


def snapshot_of(r):
    keys = ["price", "pct", "turnover", "vol_ratio", "amount", "mktcap",
            "profit_g", "rev_g", "roe", "eps", "trend20", "fund_score", "total"]
    return {k: (round(float(r[k]), 4) if k in r and pd.notna(r[k]) else None) for k in keys}


def robust_fetch_quotes_df():
    """V2 增强版全市场快照（接口/字段与 V1 完全一致，V1 文件不改动）：
    多主机轮询 + 每页8次重试 + 当日缓存兜底（缓存亦来自真实行情，标注来源）"""
    import json
    import time
    fields = "f12,f14,f2,f3,f8,f10,f6,f20"
    hosts = ["82.push2.eastmoney.com", "82.push2delay.eastmoney.com",
             "33.push2.eastmoney.com", "push2delay.eastmoney.com",
             "1.push2.eastmoney.com", "7.push2.eastmoney.com",
             "90.push2.eastmoney.com", "push2.eastmoney.com"]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
               "Referer": "https://quote.eastmoney.com/"}
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    rows, page, total = [], 1, None
    sess = requests.Session()
    sess.headers.update(headers)
    while True:
        params = {"pn": page, "pz": 200, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                  "fid": "f12", "fs": fs, "fields": fields}
        got = None
        for attempt in range(8):
            host = hosts[(page - 1 + attempt) % len(hosts)]
            try:
                r = sess.get(f"https://{host}/api/qt/clist/get", params=params, timeout=20)
                j = r.json().get("data")
                if j and j.get("diff") is not None:
                    got = j
                    break
            except Exception:
                time.sleep(min(2 + attempt, 6))
        if got is None:
            hs.log_event(TASK, "WARN", f"快照第{page}页8次重试均失败，已获取{len(rows)}条")
            break
        diff = got["diff"]
        if isinstance(diff, dict):
            diff = list(diff.values())
        rows.extend(diff)
        total = got.get("total") or total
        if (total and len(rows) >= total) or not diff:
            break
        page += 1
    if len(rows) >= 1000:
        df = pd.DataFrame(rows).rename(columns={
            "f12": "code", "f14": "name", "f2": "price", "f3": "pct",
            "f8": "turnover", "f10": "vol_ratio", "f6": "amount", "f20": "mktcap"})
        os.makedirs("data/cache", exist_ok=True)
        df.to_csv(f"data/cache/snapshot_{hs.yyyymmdd()}.csv", index=False)
        print(f"全市场快照: {len(rows)} 只（实时）", flush=True)
        return df
    # 当日缓存兜底（真实历史行情，明确标注）
    cache = f"data/cache/snapshot_{hs.yyyymmdd()}.csv"
    if os.path.exists(cache):
        hs.log_event(TASK, "WARN", f"实时快照不完整({len(rows)}条)，使用当日缓存快照 {cache}")
        print(f"⚠️ 使用当日缓存快照: {cache}", flush=True)
        return pd.read_csv(cache, dtype={"code": str})
    raise RuntimeError(f"全市场快照拉取不完整: 仅 {len(rows)} 条，且无当日缓存")


def main():
    v1.fetch_quotes_df = robust_fetch_quotes_df  # V2侧替换数据获取，V1过滤算法不变
    conn = hs.get_conn()
    run_date = hs.today()
    env = market_env()
    try:
        snap = v1.load_snapshot()
        fund = v1.load_fundamentals(snap)
        codes = fund["code"].tolist()
        log(f"开始拉取 {len(codes)} 只候选K线判断趋势...")
        import concurrent.futures as cf
        passed = {}
        with cf.ThreadPoolExecutor(max_workers=12) as ex:
            for res in ex.map(v1.trend_ok, codes):
                if res:
                    passed[res[0]] = res[1:]
        log(f"趋势过滤后: {len(passed)} 只")
        counts = {"universe": len(snap), "pass1": len(snap), "pass2": len(fund), "pass3": len(passed)}

        if not passed:
            hs.save_run(conn, TASK, run_date, MODEL_VERSION, "success", counts, env, hs._ts())
            hs.log_event(TASK, "INFO", "今日无同时满足业绩+趋势的候选")
            if "--no-push" not in sys.argv:
                v1.push("今日无同时满足「业绩质量+趋势多头」的候选，建议观望。")
            return

        final = fund[fund["code"].isin(passed.keys())].copy()
        final["trend20"] = final["code"].map(lambda c: passed[c][2])
        final["total"] = final["fund_score"] * 0.65 + (30 - final["trend20"].clip(0, 30)) / 30 * 35
        top = final.sort_values("total", ascending=False).head(10)

        industry = v1.get_industry_map(top["code"].tolist())
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

        run_id = hs.save_run(conn, TASK, run_date, MODEL_VERSION, "success", counts, env, hs._ts())

        # 候选归档：业绩过滤后（fundamental）+ 趋势通过（trend）
        cand_rows = []
        for _, r in fund.iterrows():
            cand_rows.append({"code": r["code"], "name": r["name"], "stage": "fundamental",
                              "close_price": r["price"], "score": round(r["fund_score"], 3),
                              "industry": industry.get(r["code"]), "snapshot": snapshot_of(r)})
        for _, r in final.iterrows():
            cand_rows.append({"code": r["code"], "name": r["name"], "stage": "trend",
                              "close_price": r["price"], "score": round(r["total"], 3),
                              "industry": industry.get(r["code"]), "snapshot": snapshot_of(r)})
        n_cand = hs.save_candidates(conn, run_id, cand_rows)

        # Top10 归档
        pick_rows = []
        for i, (_, r) in enumerate(top.iterrows(), 1):
            ind = industry.get(r["code"], "其他")
            pick_rows.append({
                "code": r["code"], "name": r["name"], "rank": i,
                "score": round(r["total"], 3),
                "scores": {"fund_score": round(r["fund_score"], 3), "trend20": round(r["trend20"], 2),
                           "profit_g": round(r["profit_g"], 2), "rev_g": round(r["rev_g"], 2),
                           "roe": round(r["roe"], 2)},
                "reason": (f"净利+{r['profit_g']:.0f}%/营收+{r['rev_g']:.0f}%双增，ROE {r['roe']:.1f}%；"
                           f"收盘>MA20>MA60多头，20日涨幅{r['trend20']:.1f}%≤15%；行业分散[{ind}]"),
                "risk_tags": risk_tags(r),
                "close_price": r["price"], "snapshot": snapshot_of(r), "industry": ind})
        n_pick = hs.save_picks(conn, run_id, TASK, run_date, MODEL_VERSION, pick_rows)

        # 文件归档（按日目录，历史日期不覆盖）
        hs.archive_json(run_date, "quality_top10.json",
                        {"run_id": run_id, "model_version": MODEL_VERSION, "market_env": env,
                         "data_updated_at": hs._ts(), "picks": pick_rows})
        hs.archive_json(run_date, "quality_candidates.json",
                        {"run_id": run_id, "count": len(cand_rows), "candidates": cand_rows})
        hs.archive_text(run_date, "quality_top10.txt", ",".join(top["code"].tolist()))
        log(f"归档完成: 新增候选 {n_cand} 条, 新增Top10 {n_pick} 条 (重复运行时为0)")
        log(f"Top10: {','.join(top['code'].tolist())}")

        # 兼容 V1 的 AI 下游文件
        with open("data/screened_top10.txt", "w") as f:
            f.write(",".join(top["code"].tolist()))

        if "--save-only" in sys.argv:
            print("SAVE_OK")
            return
        if "--no-push" in sys.argv:
            print("NO_PUSH_OK")
            return

        lines = [f"💎 **质量选股 Top{len(top)}** {hs.now_bj().strftime('%m-%d %H:%M')}\n",
                 "三重把关：业绩双增+ROE | 多头趋势(追高≤15%) | 行业分散\n"]
        for i, (_, r) in enumerate(top.iterrows(), 1):
            ind = industry.get(r["code"], "")
            lines.append(f"{i}. **{r['name']}**({r['code']}) {r['price']:.2f} {r['pct']:+.2f}% [{ind}]")
            lines.append(f"   净利+{r['profit_g']:.0f}% 营收+{r['rev_g']:.0f}% ROE {r['roe']:.1f}% "
                         f"| 20日涨{r['trend20']:.0f}% 市值{r['mktcap']/1e8:.0f}亿")
        wl = snap[snap["code"].isin(v1.WATCHLIST)]
        if not wl.empty:
            lines.append("\n**📋 自选股表现**")
            for _, r in wl.iterrows():
                e = "🔴" if r["pct"] > 0 else "🟢"
                lines.append(f"{e} {r['name']} {r['pct']:+.2f}%")
        lines.append("\n> ⚠️ 质量筛选仅供参考，不构成投资建议")
        v1.push("\n".join(lines))
    except Exception as e:
        import traceback
        hs.save_run(conn, TASK, run_date, MODEL_VERSION, "failed", error_msg=str(e)[:500])
        hs.log_event(TASK, "ERROR", f"质量选股失败: {e}\n{traceback.format_exc()[-800:]}")
        raise


if __name__ == "__main__":
    main()
