# -*- coding: utf-8 -*-
"""盘前数据准备（Stage 1 新增，V2 独立文件）

北京 9:15 运行：拉取全市场真实行情快照 -> 完整性硬校验 -> 写当日缓存 ->
按日归档元数据与统计 -> 输出业务执行摘要。

只做数据准备，不选股、不推送、不训练模型。
缓存回传仓库后，9:25 / 9:35 的工作流可复用同一数据日期的真实快照，
减少重复请求，同时避免海外服务器在开盘瞬间打接口失败。
"""
import json
import sys

import history_store as hs
import market_data as md

TASK = "premarket_data"
MODEL_VERSION = "v2.0-stage1"


def main():
    conn = hs.get_conn()
    run_date = hs.today()
    try:
        exp_date, exp_basis = md.expected_data_date()
        print(f"[盘前数据准备] 北京时间 {hs._ts()} | 期望数据日期={exp_date} 基准={exp_basis}",
              flush=True)

        # 强制实时拉取，不复用缓存，确保是本次真实获取
        df, meta = md.fetch_snapshot(use_cache=False)

        stats = {
            "expected_data_date": exp_date,
            "expected_basis": exp_basis,
            "source": meta["source"],
            "data_date": meta["data_date"],
            "date_source": meta["date_source"],
            "quote_basis": meta["quote_basis"],
            "universe_total": meta["universe_total"],
            "rows": meta["rows"],
            "complete": meta["complete"],
            "fetched_at": meta["fetched_at"],
            "price_basis": meta["price_basis"],
            "amount_unit": meta["amount_unit"],
            "mktcap_unit": meta["mktcap_unit"],
            "code_rule": meta["code_rule"],
            "board_counts": {p: int(df["code"].str.startswith(p).sum())
                             for p in ["00", "30", "60", "68"]},
            "missing_field_counts": {
                c: int(df[c].isna().sum())
                for c in md.FIELDS if c not in ("code", "name", "source")},
        }
        if "raw_total_including_bj" in meta:
            stats["raw_total_including_bj"] = meta["raw_total_including_bj"]
            stats["excluded_beijing"] = meta["excluded_beijing"]
            stats["universe_scope"] = meta["universe_scope"]

        hs.archive_json(run_date, "market_snapshot_meta.json", meta)
        hs.archive_json(run_date, "market_snapshot_stats.json", stats)
        rid = hs.save_run(conn, TASK, run_date, MODEL_VERSION, "success",
                          {"universe": meta["rows"]}, stats, hs._ts())
        # 把本次运行的行情元数据（来源/日期/基准/条数）也导出为不可变归档，保证可重建
        hs.export_run_snapshot(conn, run_date, TASK, rid)
        hs.log_event(TASK, "INFO", "盘前数据准备完成: " + json.dumps(stats, ensure_ascii=False))

        print("=== 盘前数据准备 · 业务执行摘要 ===", flush=True)
        print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)

        if not meta["complete"] or meta["rows"] < md.MIN_UNIVERSE:
            print("PREMARKET_FAIL 全市场数据不完整，后续选股应终止", flush=True)
            sys.exit(1)
        print(f"PREMARKET_OK 全市场实际获取 {meta['rows']} 只 "
              f"(来源={meta['source']}, 数据日期={meta['data_date']})", flush=True)
    except Exception as e:
        import traceback
        try:
            hs.save_run(conn, TASK, run_date, MODEL_VERSION, "failed", error_msg=str(e)[:500])
        except Exception:
            pass
        hs.log_event(TASK, "ERROR", f"盘前数据准备失败: {e}\n{traceback.format_exc()[-800:]}")
        print(f"PREMARKET_ERROR: {e}", flush=True)
        raise


if __name__ == "__main__":
    main()
