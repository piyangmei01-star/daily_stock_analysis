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


# 本次行情快照的完整元数据（来源/日期/基准/口径），随名单一起持久化，保证可追溯
_SNAPSHOT_META = {}


def robust_fetch_quotes_df():
    """全市场快照统一入口（V1 算法文件零改动，仅替换数据获取函数）。

    走 market_data 统一数据层：东财主源 -> 新浪备用源；统一代码/日期/复权/字段；
    全市场完整性硬校验（>=5000 只且 >=total*0.95）；数据日期与行情基准严格匹配；
    失败抛 MarketDataError 并写 ERROR 日志，绝不返回 200 条残缺快照或历史缓存。
    """
    import market_data as md
    df, meta = md.fetch_snapshot()
    _SNAPSHOT_META.clear()
    _SNAPSHOT_META.update(meta)
    hs.log_event(TASK, "INFO",
                 f"行情快照 source={meta['source']} rows={meta['rows']} "
                 f"total={meta['universe_total']} data_date={meta['data_date']} "
                 f"basis={meta['quote_basis']} date_source={meta['date_source']} "
                 f"fetched_at={meta['fetched_at']} price_basis={meta['price_basis']}")
    print(f"全市场快照: {len(df)} 只 | 来源={meta['source']} | 数据日期={meta['data_date']} "
          f"| 基准={meta['quote_basis']} | 完整={meta['complete']}", flush=True)
    return df


# V2 侧容错趋势判断：MA20/MA60/追高≤15% 逻辑与 V1 完全一致，
# 仅替换数据获取为多源；V1 文件零改动。统计用于硬校验，防止残缺数据冒充完整选股。
import threading
_KLINE_LOCK = threading.Lock()
_KLINE_STAT = {"ok": 0, "fail": 0, "sources": {}, "adjustments": {}}


def trend_ok_v2(code):
    import market_data as md
    try:
        close, src, adj, ddate = md.fetch_daily_kline(code)
    except Exception:
        with _KLINE_LOCK:
            _KLINE_STAT["fail"] += 1
        return None
    with _KLINE_LOCK:
        _KLINE_STAT["ok"] += 1
        _KLINE_STAT["sources"][src] = _KLINE_STAT["sources"].get(src, 0) + 1
        _KLINE_STAT["adjustments"][adj] = _KLINE_STAT["adjustments"].get(adj, 0) + 1
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    c = close.iloc[-1]
    chg20 = (c / close.iloc[-21] - 1) * 100
    if c > ma20 > ma60 and 0 <= chg20 <= 15:
        return code, ma20, ma60, chg20
    return None

def main():
    v1.fetch_quotes_df = robust_fetch_quotes_df  # V2侧替换数据获取，V1过滤算法不变
    v1.trend_ok = trend_ok_v2  # V2侧替换日K数据获取，MA/趋势判断逻辑与V1完全一致
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
        tot = _KLINE_STAT["ok"] + _KLINE_STAT["fail"]
        rate = (_KLINE_STAT["ok"] / tot) if tot else 0.0
        env["kline_integrity"] = {"ok": _KLINE_STAT["ok"], "total": tot,
                                  "rate": round(rate, 4),
                                  "sources": dict(_KLINE_STAT["sources"]),
                                  "adjustments": dict(_KLINE_STAT["adjustments"])}
        hs.log_event(TASK, "INFO",
                     f"日K完整性 ok={_KLINE_STAT['ok']}/{tot} 成功率={rate:.1%} "
                     f"来源={_KLINE_STAT['sources']} 复权口径={_KLINE_STAT['adjustments']}")
        print(f"日K完整性: ok={_KLINE_STAT['ok']}/{tot} 成功率={rate:.1%} "
              f"来源={_KLINE_STAT['sources']} 口径={_KLINE_STAT['adjustments']}", flush=True)
        if tot and rate < 0.80:
            hs.log_event(TASK, "ERROR", f"日K成功率{rate:.1%}<80%，拒绝产出残缺Top10名单")
            raise RuntimeError(f"日K数据不完整(成功率{rate:.1%})，拒绝用残缺数据冒充完整选股")
        counts = {"universe": len(snap), "pass1": len(snap), "pass2": len(fund), "pass3": len(passed)}

        if not passed:
            snap_meta = dict(_SNAPSHOT_META)
            run_meta = {"run_time_bj": hs._ts(), "data_date": snap_meta.get("data_date"),
                        "data_source": snap_meta.get("source"),
                        "quote_basis": snap_meta.get("quote_basis"),
                        "date_source": snap_meta.get("date_source"),
                        "model_version": MODEL_VERSION, "universe": counts.get("universe"),
                        "counts": counts, "market_env": env}
            rid0 = hs.save_run(conn, TASK, run_date, MODEL_VERSION, "success", counts, env, hs._ts())
            push_text0 = "今日无同时满足「业绩质量+趋势多头」的候选，建议观望。"
            # 空名单也必须独立落盘，保证每次运行都可追溯
            fp0, _ = hs.finalize_run(conn, TASK, run_date, rid0, [], run_meta,
                                     push_text=push_text0)
            hs.log_event(TASK, "INFO", f"今日无同时满足业绩+趋势的候选；空名单已归档 {os.path.basename(fp0)}")
            hs.export_run_snapshot(conn, run_date, TASK, rid0)
            if "--no-push" not in sys.argv and "--save-only" not in sys.argv:
                ret0 = None
                try:
                    ret0 = v1.push(push_text0)
                    push_ok0 = True if ret0 is None else bool(ret0)
                except Exception as e:
                    push_ok0 = False
                    hs.log_event(TASK, "ERROR", f"微信推送异常: {e}")
                hs.save_push_receipt(run_date, rid0, push_text0, push_ok0,
                                     detail={"picks": 0, "push_return": str(ret0)})
                # 修复"假绿"缺陷：空名单推送失败同样必须显式失败退出
                if not push_ok0:
                    log("PUSH_FAIL: 微信推送失败，按验收纪律退出码非0（空名单真相源已落盘）")
                    sys.exit(1)
                print("PUSH_OK")
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

        snap_meta = dict(_SNAPSHOT_META)
        run_meta = {
            "run_time_bj": hs._ts(),
            "data_date": snap_meta.get("data_date"),
            "data_source": snap_meta.get("source"),
            "quote_basis": snap_meta.get("quote_basis"),
            "date_source": snap_meta.get("date_source"),
            "model_version": MODEL_VERSION,
            "universe": counts.get("universe"),
            "counts": counts,
            "market_env": env,
        }
        # 每次运行一个全局唯一 run_id，绝不复用/覆盖历史批次
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

        # Top10 名单（与推送文本同源，不重新排序/计算）
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
        n_pick = hs.save_picks(conn, run_id, TASK, run_date, MODEL_VERSION, pick_rows,
                               meta=run_meta)

        # 推送文本：与 pick_rows 完全同源（同一个 top DataFrame）
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
        push_text = "\n".join(lines)

        # ★ 立即持久化：推送之前写入不可覆盖独立真相源 run_<run_id>.json
        # 即使随后进程崩溃或归档冲突，这份原始名单也已独立落盘、可恢复
        run_fp, run_obj = hs.finalize_run(conn, TASK, run_date, run_id, pick_rows,
                                          run_meta, push_text=push_text)
        log(f"真相源已落盘(推送前): {os.path.basename(run_fp)}")
        log(f"run_id={run_id} codes_hash={run_obj['codes_hash'][:16]}")

        # 派生「最新视图」文件（可更新；下游 AI/验收读取。真相源是上面的独立文件）
        hs.archive_json(run_date, "quality_top10.json",
                        {"run_id": run_id, "model_version": MODEL_VERSION, "market_env": env,
                         "data_date": run_meta["data_date"], "data_source": run_meta["data_source"],
                         "data_updated_at": hs._ts(), "picks": pick_rows})
        hs.archive_json(run_date, "quality_candidates.json",
                        {"run_id": run_id, "count": len(cand_rows), "candidates": cand_rows})
        hs.archive_text(run_date, "quality_top10.txt", ",".join(top["code"].tolist()))
        hs.export_run_snapshot(conn, run_date, TASK, run_id)
        with open("data/screened_top10.txt", "w") as f:
            f.write(",".join(top["code"].tolist()))
        log(f"归档完成: 新增候选 {n_cand} 条, 新增Top10 {n_pick} 条 (重复运行时为0)")
        log(f"Top10: {','.join(top['code'].tolist())}")

        if "--save-only" in sys.argv:
            print("SAVE_OK")
            return
        if "--no-push" in sys.argv:
            print("NO_PUSH_OK")
            return

        # 推送前二次校验：db 名单必须与不可覆盖真相源完全一致，否则拒绝推送
        db_codes = [r[0] for r in conn.execute(
            "SELECT code FROM picks WHERE run_id=? ORDER BY rank", (run_id,))]
        if db_codes != run_obj["codes"]:
            hs.log_event(TASK, "ERROR", "推送前一致性校验失败: db名单≠归档真相源，拒绝推送")
            raise RuntimeError("推送名单与归档名单不一致，拒绝推送")

        ret = None
        try:
            ret = v1.push(push_text)
            push_ok = True if ret is None else bool(ret)
        except Exception as e:
            push_ok = False
            hs.log_event(TASK, "ERROR", f"微信推送异常: {e}")
        hs.save_push_receipt(run_date, run_id, push_text, push_ok,
                             detail={"picks": len(pick_rows), "push_return": str(ret)})
        log(f"推送凭证已保存: push_ok={push_ok} run_id={run_id}")
        # 修复"假绿"缺陷：推送失败必须显式失败退出，Actions 才能真实反映业务结果
        if not push_ok:
            log("PUSH_FAIL: 微信推送失败，按验收纪律退出码非0（真相源与推送凭证已落盘）")
            sys.exit(1)
        print("PUSH_OK")
    except Exception as e:
        import traceback
        hs.save_run(conn, TASK, run_date, MODEL_VERSION, "failed", error_msg=str(e)[:500])
        hs.log_event(TASK, "ERROR", f"质量选股失败: {e}\n{traceback.format_exc()[-800:]}")
        raise


if __name__ == "__main__":
    main()
