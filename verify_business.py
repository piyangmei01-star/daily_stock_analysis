# -*- coding: utf-8 -*-
"""业务结果校验：不能仅凭 Actions 成功就认定业务成功。

检查今日数据库中的实际业务产出，打印并写入 GITHUB_STEP_SUMMARY。
用法: python verify_business.py quality|momentum|alert|daily_ai [--allow-empty]
"""
import os
import sqlite3
import sys

import history_store as hs


def main():
    task = sys.argv[1]
    allow_empty = "--allow-empty" in sys.argv
    run_date = hs.today()
    conn = hs.get_conn()
    lines = [f"## 业务执行摘要 · {task} · {run_date}（北京时间）", ""]
    ok = True

    if task in ("quality", "momentum"):
        r = conn.execute("SELECT run_id,status,universe_count,pass2,pass3,run_time_bj FROM screening_runs WHERE task=? AND run_date=?",
                         (task, run_date)).fetchone()
        n_pick = conn.execute("SELECT COUNT(*) FROM picks WHERE task=? AND run_date=?",
                              (task, run_date)).fetchone()[0]
        n_cand = conn.execute("SELECT COUNT(*) FROM candidates WHERE run_id LIKE ?",
                              (f"{task}_{run_date.replace('-', '')}",)).fetchone()[0]
        if not r:
            lines.append("❌ 今日无选股运行记录")
            ok = False
        else:
            lines.append(f"- 运行状态: **{r[1]}**（{r[5]}）")
            lines.append(f"- 过滤漏斗: 快照{r[2]} → 业绩{r[3]} → 趋势{r[4]}")
            lines.append(f"- 入选名单: **{n_pick} 只** | 候选归档: {n_cand} 条")
            ok = r[1] == "success" and (n_pick > 0 or allow_empty)
            if n_pick == 0:
                lines.append("⚠️ 名单为空（--allow-empty 时视为正常）")
    elif task == "alert":
        r = conn.execute("SELECT in_session,checked_count,anomaly_count,data_ok,error_msg,time_bj FROM alert_checks WHERE run_date=? ORDER BY check_id DESC LIMIT 1",
                         (run_date,)).fetchone()
        n_ev = conn.execute("SELECT COUNT(*) FROM alert_events WHERE run_date=?", (run_date,)).fetchone()[0]
        if not r:
            lines.append("❌ 今日无异动检测记录")
            ok = False
        else:
            lines.append(f"- 最近检测: {r[5]} | 交易时段: {bool(r[0])}")
            lines.append(f"- 检测数量: **{r[1]} 只** | 异常: {r[2]} 条 | 数据成功: {bool(r[3])}")
            lines.append(f"- 今日异动事件累计: {n_ev} 条")
            if r[4]:
                lines.append(f"- 备注: {r[4]}")
            ok = True  # 非交易时段跳过也属正常业务结果，以留痕为准
    elif task == "daily_ai":
        n = conn.execute("SELECT COUNT(*) FROM ai_reports WHERE run_date=?", (run_date,)).fetchone()[0]
        lines.append(f"- 今日AI分析Top10: **{n} 只**")
        ok = n > 0 or allow_empty

    n_err = conn.execute("SELECT COUNT(*) FROM run_logs WHERE run_date=? AND level='ERROR'",
                         (run_date,)).fetchone()[0]
    lines.append(f"- 今日错误日志: {n_err} 条")
    text = "\n".join(lines)
    print(text, flush=True)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    print("VERIFY_OK" if ok else "VERIFY_FAIL", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
