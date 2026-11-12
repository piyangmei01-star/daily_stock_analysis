# -*- coding: utf-8 -*-
"""V2 强势短线推送：算法复用 V1（import morning_push），新增 Top5 历史归档。"""
import sys

import pandas as pd

import history_store as hs
import morning_push as v1

MODEL_VERSION = "v2.0-stage1"
TASK = "momentum"


def snapshot_of(r):
    keys = ["price", "pct", "turnover", "vol_ratio", "amount", "mktcap", "pe", "score"]
    return {k: (round(float(r[k]), 4) if k in r and pd.notna(r[k]) else None) for k in keys}


def risk_tags(r):
    tags = ["开盘已涨(追涨属性)"]
    if r["pct"] >= 8:
        tags.append("接近涨停(>8%)")
    if r["pct"] >= 5:
        tags.append("当日涨幅较大(>5%)")
    if r["turnover"] > 15:
        tags.append("高换手(>15%)")
    if r["vol_ratio"] >= 5:
        tags.append("量比异常放大(>5)")
    if r["pe"] > 80:
        tags.append("高估值(PE>80)")
    return ";".join(tags)


def main():
    conn = hs.get_conn()
    run_date = hs.today()
    try:
        import market_data as md
        df, meta = md.fetch_snapshot()
        hs.log_event(TASK, "INFO",
                     f"行情快照 source={meta['source']} rows={meta['rows']} "
                     f"data_date={meta['data_date']} basis={meta['quote_basis']}")
        # 新浪备用源不提供量比：先用其余硬条件缩小候选域，再由腾讯批量补齐。
        # 不允许把「量比缺失」静默当作 0 而产出空名单或错误筛选。
        if df["vol_ratio"].isna().all():
            cand = df[~df["name"].str.contains("ST|退", na=False)]
            cand = cand[cand["code"].str.match(r"^(00|30|60)")]
            cand = cand[(cand["amount"] >= 1e8) & (cand["mktcap"] >= 3e9)
                        & (cand["price"].between(3, 200)) & (cand["pct"].between(1.0, 9.5))]
            df, n, src = md.enrich_vol_ratio_from_tencent(df, cand["code"].tolist())
            hs.log_event(TASK, "INFO", f"量比/PE补齐 {n}/{len(cand)} 只, 备用来源={src}")
            if n == 0:
                raise RuntimeError("备用源量比补齐失败，强势股筛选终止（不使用缺失量比的名单）")
        picks = v1.screen(df)

        counts = {"universe": len(df), "pass1": len(df), "pass2": len(picks), "pass3": len(picks)}
        run_id = hs.save_run(conn, TASK, run_date, MODEL_VERSION, "success",
                             counts, {"captured_at": hs._ts()}, hs._ts())

        rows = []
        for i, (_, r) in enumerate(picks.iterrows(), 1):
            rows.append({
                "code": r["code"], "name": r["name"], "rank": i,
                "score": round(r["score"], 3),
                "scores": {"vol_ratio": round(float(r["vol_ratio"]), 2),
                           "turnover": round(float(r["turnover"]), 2),
                           "pct": round(float(r["pct"]), 2)},
                "reason": (f"量比{r['vol_ratio']:.1f}≥1.5放量，换手{r['turnover']:.1f}%，"
                           f"涨幅{r['pct']:+.2f}%，成交额{r['amount']/1e8:.1f}亿，PE {r['pe']:.1f}"),
                "risk_tags": risk_tags(r),
                "close_price": r["price"], "snapshot": snapshot_of(r), "industry": None})
        n_pick = hs.save_picks(conn, run_id, TASK, run_date, MODEL_VERSION, rows)
        n_cand = hs.save_candidates(conn, run_id, [
            {"code": r["code"], "name": r["name"], "stage": "screened",
             "close_price": r["price"], "score": round(r["score"], 3),
             "snapshot": snapshot_of(r)} for _, r in picks.iterrows()])
        hs.archive_json(run_date, "momentum_top5.json",
                        {"run_id": run_id, "model_version": MODEL_VERSION,
                         "data_updated_at": hs._ts(), "picks": rows})
        print(f"归档完成: 新增Top5 {n_pick} 条, 候选 {n_cand} 条 (重复运行时为0)", flush=True)
        hs.export_run_snapshot(conn, run_date, TASK)  # 归档真相源: append-only JSON

        if "--no-push" in sys.argv:
            print("NO_PUSH_OK")
            return
        lines = [f"☀️ **盘前选股清单** {hs.now_bj().strftime('%m-%d %H:%M')}\n"]
        if picks.empty:
            lines.append("今日无符合条件的候选（市场过冷或条件过严），建议观望。")
        else:
            lines.append("**🔍 今日候选 Top 5**（量能+动量+热度综合评分）\n")
            for i, (_, r) in enumerate(picks.iterrows(), 1):
                lines.append(f"{i}. **{r['name']}**({r['code']}) {r['price']:.2f} "
                             f"{r['pct']:+.2f}% | 量比{r['vol_ratio']:.1f} 换手{r['turnover']:.1f}% "
                             f"成交{r['amount']/1e8:.1f}亿 | 评分{r['score']:.0f}")
        lines.append("\n**📋 你的自选股表现**\n")
        lines.extend(v1.watch_performance(df))
        lines.append("\n> ⚠️ 量化筛选仅供参考，不构成投资建议")
        ok = v1.push("\n".join(lines))
        print("PUSH_OK" if ok else "PUSH_FAIL")
    except Exception as e:
        import traceback
        hs.save_run(conn, TASK, run_date, MODEL_VERSION, "failed", error_msg=str(e)[:500])
        hs.log_event(TASK, "ERROR", f"强势股选股失败: {e}\n{traceback.format_exc()[-800:]}")
        raise


if __name__ == "__main__":
    main()
