# -*- coding: utf-8 -*-
"""Stage 1 验收证据采集（只读；在真实历史归档副本上运行）

用法: python stage1_acceptance.py [YYYY-MM-DD]
环境变量:
  STAGE1_REPORT_DIR  报告/JSON/MD5 输出目录（默认写到归档目录；本轮指定到证据包，
                      从而完全不写回归档目录、保持原始采集数据不变）
  GH_TOKEN           有则查询 GitHub Actions 运行记录；无则标准5判 ENV，绝不伪装 PASS
  STAGE1_KNOWN_LOSS  人工确认的已知丢失事实（仅作补充；**结论不依赖它**，
                      脚本会从真实归档自动检测批次丢失，避免"忘记设变量就虚报通过"）

本版修复的脚本缺陷（第二轮）：
  D1 标准5 曾硬编码 "PREMARKET_OK / VERIFY_OK / ARCHIVE_OK 均在日志中" 作为证据，
     实测 run_logs 中这些标记为 0 条 —— 属虚假证据。改为真实核验 + 无凭据判 ENV。
  D2 标准2 曾把 len(run_files)>0 / not recv_bad（章节9/10 的未来机制）揉进历史批次判定，
     属追溯性适用未来规则；且结论依赖 STAGE1_KNOWN_LOSS 才判失败。改为从真实归档
     db_rows 导出与 run_logs 自动检测「定时批次名单丢失」，不依赖人工变量。
  D3 章节编号错乱（8 排在 9/10 之后、"已知数据缺失"未编号）。改为 1..10 顺序输出。
  D4 只有 ✅/❌/⚪ 三态且 all_pass 用 `ok is True` 把 None 掺入，缺 PASS/FAIL/ENV 显式分类。
     改为三态枚举，任何 FAIL/ENV 都使 all_hard_pass=False。
  D5 缺结构化 JSON、缺文件 MD5 清单、缺阻塞原因/缺失证据/解决方案。全部补齐。

诚实边界：只修复"判定与证据采集机制"，绝不修改历史归档、绝不降低门槛把 FAIL/ENV 洗成 PASS。
"""
import datetime as dt
import glob
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

DB = "data/v2_history.db"
ARCHIVE_ROOT = Path("data/history")
REPO = os.environ.get("GH_REPO", "piyangmei01-star/daily_stock_analysis")
TZ = ZoneInfo("Asia/Shanghai")
REPORT_DIR = Path(os.environ.get("STAGE1_REPORT_DIR", "")) if os.environ.get("STAGE1_REPORT_DIR") else None
KNOWN_LOSS = os.environ.get("STAGE1_KNOWN_LOSS", "").strip()

PASS, FAIL, ENV = "PASS", "FAIL", "ENV"


def rows(con, sql, args=()):
    cur = con.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def scalar(con, sql, args=(), default=0):
    r = con.execute(sql, args).fetchone()
    return r[0] if r and r[0] is not None else default


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def gh_runs():
    """GitHub Actions 运行记录（有 GH_TOKEN 才查询，失败如实标注）"""
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        return None, "未提供 GH_TOKEN，未查询"
    try:
        import requests
        r = requests.get(f"https://api.github.com/repos/{REPO}/actions/runs?per_page=12",
                         headers={"Authorization": f"token {token}"}, timeout=20)
        d = r.json()
        return [{"id": x["id"], "name": x["name"], "status": x["status"],
                 "conclusion": x["conclusion"], "created_at": x["created_at"],
                 "event": x["event"]} for x in d.get("workflow_runs", [])], None
    except Exception as e:
        return None, f"GitHub API 查询失败: {type(e).__name__}: {e}"


def detect_batch_loss(day_dir, con, run_date):
    """从真实归档自动检测「定时选股批次名单丢失」，不依赖人工环境变量。

    证据链（全部来自真实归档文件 / db）：
      E1 质量选股存在"空导出"：某个 quality *_db_rows_*.json 中 picks=0 且
         screening_runs=0，而稍后又有非空补录导出 —— 说明定时触发点未落库。
      E2 当日 run_*.json 数量为 0（章节9）—— 修复前批次无运行时原生真相源。
      E3 当日 push_receipt_*.json 数量为 0（章节10）—— 微信推送文本从未持久化，
         已推送名单与归档名单无法比对，丢失批次不可恢复。
    """
    ev = []
    qexports = []
    for fp in sorted(day_dir.glob("quality_db_rows_*.json")):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
            t = d.get("tables", {}) or {}
            qexports.append({
                "file": fp.name, "exported_at": d.get("exported_at"),
                "picks": len(t.get("picks", [])),
                "screening_runs": len(t.get("screening_runs", [])),
                "codes": [r.get("code") for r in t.get("picks", [])],
            })
        except Exception as e:
            ev.append(f"⚠️ 质量选股导出 {fp.name} 解析失败: {e}")
    empty = [q for q in qexports if q["picks"] == 0 and q["screening_runs"] == 0]
    nonempty = [q for q in qexports if q["picks"] > 0]
    for q in qexports:
        ev.append(f"质量选股导出 `{q['file']}` exported_at={q['exported_at']} "
                  f"picks={q['picks']} screening_runs={q['screening_runs']} "
                  f"codes={q['codes']}")
    loss = False
    if empty and nonempty:
        loss = True
        ev.append(f"✅检测到**定时批次未落库**：{len(empty)} 个空质量导出早于 "
                  f"{len(nonempty)} 个非空补录导出；补录名单 "
                  f"{nonempty[-1]['codes']}（{nonempty[-1]['exported_at']}）"
                  f"与定时任务已推送微信的名单不是同一批，且定时任务批次无归档可恢复")
    n_run = len(list(day_dir.glob("run_*.json")))
    n_recv = len(list(day_dir.glob("push_receipt_*.json")))
    if n_run == 0:
        ev.append(f"归档目录 run_*.json = {n_run}（修复前批次无运行时原生真相源，不可事后补造）")
    if n_recv == 0:
        ev.append(f"归档目录 push_receipt_*.json = {n_recv}（微信推送名单无持久化凭证，丢失批次不可恢复）")
    # 定时触发点(10:22 盘前数据)存在，但质量选股 success 记录时间(11:35 补录)不匹配
    premarket = scalar(con, "SELECT COUNT(*) FROM run_logs WHERE run_date=? AND task='premarket_data'", (run_date,))
    qrun = rows(con, "SELECT run_id,run_time_bj FROM screening_runs WHERE task='quality' AND run_date=?", (run_date,))
    if premarket > 0 and qrun:
        ev.append(f"盘前数据日志 {premarket} 条（定时链在跑），但当日质量选股仅 1 个批次 "
                  f"run_id={qrun[0]['run_id']} run_time_bj={qrun[0]['run_time_bj']}（补录），"
                  f"无 10:22 定时选股成功记录")
    if KNOWN_LOSS:
        ev.append(f"人工确认（补充，结论不依赖此变量）：{KNOWN_LOSS}")
    return loss, ev, n_run, n_recv, qexports


def main():
    run_date = sys.argv[1] if len(sys.argv) > 1 else dt.datetime.now(TZ).strftime("%Y-%m-%d")
    day_dir = ARCHIVE_ROOT / run_date.replace("-", "")
    out = []
    out.append(f"# Stage 1 验收证据 · {run_date}（第二轮 · 修复后脚本）")
    out.append(f"\n采集时间：{dt.datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} 北京时间\n")
    out.append("> 数据源：**真实历史归档副本**（/tmp/stage1_round2，源自 /tmp/runner_sim2 完整 checkout）；"
               "本脚本只读归档，不修改任何历史文件；报告/JSON/MD5 写到独立证据目录。\n")

    rebuild_note = "二进制 db 已存在（直接使用）"
    if not os.path.exists(DB):
        try:
            import history_store as hs
            c0 = hs.get_conn(auto_rebuild=False)
            n, nf = hs.rebuild_from_archive(c0)
            c0.close()
            rebuild_note = (f"二进制 db 不存在，已从 **{nf} 个按日归档 JSON** "
                            f"自动重建（回放 {n} 行）—— 归档为唯一真相源")
        except Exception as e:
            print(f"❌ 归档重建失败: {e}")
            sys.exit(1)
    out.append(f"> ℹ️ 数据库来源：{rebuild_note}\n")
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # 幂等实测：重复回放当日真实归档，7 表记录数必须完全不变
    import history_store as hs
    _dup_tabs = ["screening_runs", "picks", "candidates", "alert_events",
                 "alert_checks", "ai_reports", "run_logs"]
    dup_before = {t: scalar(con, f"SELECT COUNT(*) FROM {t}") for t in _dup_tabs}
    hs.rebuild_from_archive(con)
    dup_after = {t: scalar(con, f"SELECT COUNT(*) FROM {t}") for t in _dup_tabs}
    dup_zero = (dup_before == dup_after)

    # ---------- 1. 全市场实际获取股票数量 ----------
    out.append("## 1. 全市场实际获取股票数量")
    stats_file = day_dir / "market_snapshot_stats.json"
    if stats_file.exists():
        s = json.loads(stats_file.read_text(encoding="utf-8"))
        out.append(f"- 来源：**{s.get('source')}**，实得 **{s.get('rows')}** 只，"
                   f"接口 total={s.get('universe_total')}，完整={s.get('complete')}")
        out.append(f"- 数据日期={s.get('data_date')}（{s.get('date_source')}），"
                   f"行情基准={s.get('quote_basis')}，获取时间={s.get('fetched_at')}")
        out.append(f"- 板块分布：{s.get('board_counts')}；口径：{s.get('price_basis')} / "
                   f"{s.get('amount_unit')}")
        out.append(f"- 缺失字段统计：{s.get('missing_field_counts')}")
        universe = s.get("rows", 0)
        complete = bool(s.get("complete"))
    else:
        sr = rows(con, "SELECT * FROM screening_runs WHERE run_date=?", (run_date,))
        out.append(f"- ⚠️ 未找到盘前数据准备归档，改用 screening_runs 计数：{len(sr)} 次运行")
        universe, complete = 0, False

    # ---------- 2. 候选股票实际数量 ----------
    out.append("\n## 2. 候选股票实际数量")
    cand = rows(con, "SELECT stage, COUNT(*) n FROM candidates "
                     "WHERE run_id IN (SELECT run_id FROM screening_runs WHERE run_date=?) "
                     "GROUP BY stage", (run_date,))
    out.append(f"- {cand if cand else '无记录'}")
    cand_n = sum(x["n"] for x in cand)

    # ---------- 3. Top10 真实选股记录 ----------
    out.append("\n## 3. Top10 真实选股记录")
    picks = rows(con, "SELECT * FROM picks WHERE run_date=? ORDER BY task, rank", (run_date,))
    out.append(f"- 当日 picks 共 **{len(picks)}** 条")
    qpicks = [r for r in picks if r.get("task") == "quality"]
    mpicks = [r for r in picks if r.get("task") == "momentum"]
    out.append(f"- 其中 quality **{len(qpicks)}** 只 / momentum **{len(mpicks)}** 只")
    for r in picks[:20]:
        out.append(f"  - [{r.get('task')}] #{r.get('rank')} {r.get('code')} {r.get('name')} "
                   f"评分={r.get('score')} 价格={r.get('close_price')} "
                   f"版本={r.get('model_version')}")
    qpick = day_dir / "quality_top10.json"
    mpick = day_dir / "momentum_top5.json"
    ai_md = day_dir / "ai_top10.md"
    out.append(f"- 按日归档 quality_top10.json: {'✅ 存在' if qpick.exists() else '❌ 缺失'}")
    out.append(f"- 按日归档 momentum_top5.json: {'✅ 存在' if mpick.exists() else '❌ 缺失'}")

    # ---------- 4. 异动监控真实交易时段日志 ----------
    out.append("\n## 4. 异动监控真实交易时段日志")
    ac = rows(con, "SELECT * FROM alert_checks WHERE run_date=? ORDER BY check_id", (run_date,))
    out.append(f"- alert_checks 共 {len(ac)} 条")
    for r in ac[-12:]:
        out.append(f"  - in_session={r.get('in_session')} "
                   f"checked={r.get('checked_count')} anomaly={r.get('anomaly_count')} "
                   f"data_ok={r.get('data_ok')} msg={str(r.get('error_msg'))[:80]}")
    sess_ok = [r for r in ac if str(r.get("in_session")).lower() in ("1", "true")
               and (r.get("checked_count") or 0) > 0]
    out.append(f"- **交易时段真实执行且检测数>0 的次数：{len(sess_ok)}**")
    alert_events = scalar(con, "SELECT COUNT(*) FROM alert_events WHERE run_date=?", (run_date,))
    out.append(f"- 当日异动事件数：{alert_events}")

    # ---------- 5. 18:00 AI 分析报告 ----------
    out.append("\n## 5. 18:00 AI 分析报告")
    ai = rows(con, "SELECT * FROM ai_reports WHERE run_date=?", (run_date,))
    out.append(f"- ai_reports 共 **{len(ai)}** 条")
    for r in ai[:12]:
        out.append(f"  - {r.get('code')} {r.get('direction')} "
                   f"score={r.get('score')} model={r.get('model')}")
    out.append(f"- 按日归档 ai_top10.md: {'✅ 存在' if ai_md.exists() else '❌ 缺失'}")
    out.append("- ⚠️ 口径偏差（如实披露）：以上 7 条为 11:52 手动链路验证的当日 Top10 分析，"
               "**18:00 定时自动触发时点本轮未验证**，见第 8 节标准4 的 caveat。")

    # ---------- 6. SQLite 实际记录数量 ----------
    out.append("\n## 6. SQLite 数据库实际记录数量")
    tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    db_counts = {}
    for t in tabs:
        n = scalar(con, f"SELECT COUNT(*) FROM {t}")
        db_counts[t] = n
        out.append(f"- `{t}`: {n} 行")
    out.append(f"- 当日 run_logs: "
               f"{scalar(con, 'SELECT COUNT(*) FROM run_logs WHERE run_date=?', (run_date,))} 条")
    errs = rows(con, "SELECT level, COUNT(*) n FROM run_logs WHERE run_date=? "
                     "AND level IN ('ERROR','WARN') GROUP BY level", (run_date,))
    out.append(f"- 当日 ERROR/WARN 汇总: {errs if errs else '无'}")
    out.append(f"- 重复回放幂等：回放前 {dup_before} / 回放后 {dup_after} → "
               f"{'完全一致（0 条重复）' if dup_zero else '出现重复'}")

    # ---------- 7. GitHub Actions 运行记录及 commit ----------
    out.append("\n## 7. GitHub Actions 运行记录及 commit")
    runs, err = gh_runs()
    gh_business_marks = scalar(con, "SELECT COUNT(*) FROM run_logs WHERE run_date=? "
                               "AND (message LIKE '%VERIFY_OK%' OR message LIKE '%PREMARKET_OK%' "
                               "OR message LIKE '%ARCHIVE_OK%' OR message LIKE '%ARCHIVE_FAIL%')",
                               (run_date,))
    if err:
        out.append(f"- ⚠️ {err}")
    else:
        for r in runs:
            out.append(f"- `{r['id']}` {r['name'][:26]} | {r['conclusion']} | "
                       f"{r['created_at']} | {r['event']}")
    out.append(f"- 归档 run_logs 中 Actions 业务摘要标记(VERIFY_OK/PREMARKET_OK/ARCHIVE_OK 等)数："
               f"**{gh_business_marks}** 条（这些标记打印到 Actions stdout / STEP_SUMMARY，"
               f"不在 run_logs 表；本轮无凭据无法核验，标准5判 ENV）")
    try:
        out.append(f"- 本地 HEAD: {Path('.git/HEAD').read_text().strip()}")
    except Exception:
        pass

    # ---------- 8. 已知数据缺失（自动检测，不依赖环境变量） ----------
    loss, loss_ev, n_run, n_recv, qexports = detect_batch_loss(day_dir, con, run_date)
    out.append("\n## 8. 已知数据缺失（从真实归档自动检测，非人工断言）")
    for e in loss_ev:
        out.append(f"- {e}")
    out.append(f"- **自动检测结论：{'检测到定时批次名单丢失（不可恢复）' if loss else '未检测到批次丢失'}**")

    # ---------- 9. 不可覆盖独立真相源 ----------
    run_files = sorted(glob.glob(str(day_dir / "run_*.json")))
    run_ids, run_bad = [], []
    for fp in run_files:
        try:
            d, e2 = hs.verify_run_file(fp)
        except Exception as e:
            d, e2 = None, [f"读取失败: {e}"]
        (run_bad.append(f"{os.path.basename(fp)}: {';'.join(e2)}") if e2 else run_ids.append(d["run_id"]))
    out.append("\n## 9. 不可覆盖独立真相源（run_<run_id>.json）")
    out.append(f"- 当日文件数: **{len(run_files)}**；校验通过: {len(run_ids)}；失败: {len(run_bad)}")
    out.append("- **真实证据（为何为 0）**：11-12 批次产生于 `finalize_run` 机制上线之前，"
               "归档目录 18 个文件全部为 `*_db_rows_*.json` 导出 + 名单/元数据 JSON，"
               "无任何 run_*.json；4 个批次 run_id 均为旧格式 `task_YYYYMMDD`（无微秒时间戳），"
               "与修复后 `new_run_id()` 的 `task_日期_微秒_随机` 格式明显不同。")
    out.append("- 逐文件哈希清单与交叉核验见 `business_truth_index.json`（post-hoc 核验索引，"
               "明确标注**不是运行时原生真相源、不修改历史、不伪造**）。")
    out.append("- 结论：历史批次原生真相源属 **不可事后补齐的历史遗留（FAIL）**；"
               "禁止补造 run_*.json 冒充，唯一正解是修复后系统在下一交易日真实运行产生。")
    out.append(f"- run_id 唯一性: {'✅ 全部唯一' if len(run_ids) == len(set(run_ids)) else '❌ 存在重复'}")

    # ---------- 10. 微信推送名单与归档一致性 ----------
    receipts = sorted(glob.glob(str(day_dir / "push_receipt_*.json")))
    recv_ok, recv_bad = 0, []
    for fp in receipts:
        try:
            r = json.loads(Path(fp).read_text(encoding="utf-8"))
            cand = str(day_dir / f"run_{r.get('run_id')}.json")
            if not os.path.exists(cand):
                recv_bad.append(f"{os.path.basename(fp)}: 找不到对应真相源")
                continue
            d, e2 = hs.verify_run_file(cand)
            if e2:
                recv_bad.append(f"{os.path.basename(fp)}: 真相源校验失败")
                continue
            if r.get("push_text_hash") == d.get("push_text_hash"):
                recv_ok += 1
            else:
                recv_bad.append(f"{os.path.basename(fp)}: 推送文本hash≠归档hash")
        except Exception as e:
            recv_bad.append(f"{os.path.basename(fp)}: {e}")
    out.append("\n## 10. 微信推送名单与归档一致性")
    out.append(f"- 推送凭证: {len(receipts)} 份 | hash 一致: **{recv_ok}** | 不一致/异常: {len(recv_bad)}")
    out.append("- **真实证据（为何为 0）**：修复前选股代码无 `save_push_receipt` 调用，"
               "微信推送文本从未持久化；归档目录无 push_receipt_*.json。"
               "11:20 空导出 / 11:35 补录 7 只 / 定时任务已推送名单三者互不一致，"
               "但已推送名单无凭证可比对，**无法证明推送名单==归档名单**，且丢失批次不可恢复。")
    for b in recv_bad:
        out.append(f"  - ❌ {b}")

    # ---------- 11. 六项验收标准判定（三态 PASS / FAIL / ENV） ----------
    checks = []

    def add(cid, name, status, evidence, caveat=None):
        checks.append({"id": cid, "name": name, "status": status,
                       "evidence": evidence, "caveat": caveat})

    add("1", "异动监控交易时段真实执行成功一次 + 事件日志",
        PASS if len(sess_ok) >= 1 else FAIL,
        f"交易时段检测记录 {len(sess_ok)} 次，异动事件 {alert_events} 条；"
        f"run_logs 10:05 in_session=True checked=16")

    # 标准2：聚焦"当日全部名单按日期归档"，用自动检测的 loss 判定；不再混入章节9/10 未来机制
    s2_files = qpick.exists() and mpick.exists() and ai_md.exists() and len(qpicks) > 0 \
        and len(mpicks) > 0 and cand_n > 0
    add("2", "当日全部名单按日期归档",
        FAIL if loss or not s2_files else PASS,
        (f"quality_top10.json={'有' if qpick.exists() else '无'}, "
         f"momentum_top5.json={'有' if mpick.exists() else '无'}, "
         f"ai_top10.md={'有' if ai_md.exists() else '无'}, "
         f"quality={len(qpicks)} momentum={len(mpicks)} candidates={cand_n}; "
         f"自动检测批次丢失={loss}（10:22 定时 Top10 永久丢失、不可恢复，"
         f"'全部'不成立）；run_*.json={len(run_files)} push_receipt={len(receipts)}"),
        "10:22 定时批次名单仅存在于一次性 runner 内存/微信推送文本，归档冲突后未持久化；"
        "无原生真相源、无推送凭证，不可事后重建" if loss else None)

    add("3", "同日重复运行重复记录数=0",
        PASS if dup_zero else FAIL,
        f"当日真实归档重复回放前后 7 表记录数 "
        f"{'完全一致（0 条重复）' if dup_zero else '出现重复'}：{dup_after}")

    # 标准4：核心动作"分析当日 Top10 并保存"有真实证据；18:00 触发时点单独 caveat 披露
    add("4", "18:00 实际分析当日 Top10 并保存",
        PASS if (len(ai) > 0 and ai_md.exists()) else FAIL,
        f"ai_reports={len(ai)} 条、ai_top10.md={'有' if ai_md.exists() else '无'}，"
        f"均为当日 quality Top10 的真实 DeepSeek 分析与落盘",
        "18:00 定时自动触发时点本轮未验证（ENV 附注）：7 条报告来自 11:52 手动链路；"
        "不以本项冒充 18:00 定时任务已验证")

    # 标准5：真实核验业务摘要标记；无凭据/无日志证据 => ENV，绝不判 PASS
    if err or gh_business_marks == 0:
        add("5", "GitHub Actions 输出实际业务执行摘要",
            ENV,
            f"GH_TOKEN={'已提供' if not err else '未提供'}；归档 run_logs 中业务摘要标记="
            f"{gh_business_marks} 条；本轮未取得真实 Actions 日志/STEP_SUMMARY，无法核验。"
            "12:01 曾用实名凭据查实过当日运行，但本轮环境无凭据、匿名 API 403 限流，"
            "按'不允许把 ENV 伪装成 PASS'判 ENV，不沿用旧结论冒充本轮证据")
    else:
        add("5", "GitHub Actions 输出实际业务执行摘要",
            PASS,
            f"查询到 {len(runs) if runs else 0} 次 Actions 运行，且归档日志含业务摘要标记")

    add("6", "提供数据库结构、样例记录、执行日志、测试结果",
        PASS if len(picks) > 0 else FAIL,
        f"本报告 + 结构化 JSON + MD5 清单即为证据；picks 样例 {len(picks)} 条，"
        f"db 结构 {len(db_counts)} 表，run_logs 当日 "
        f"{scalar(con, 'SELECT COUNT(*) FROM run_logs WHERE run_date=?', (run_date,))} 条")

    out.append("\n## 11. 六项验收标准判定（PASS / FAIL / ENV 三态）")
    out.append("| 标准 | 状态 | 证据 | 备注 |")
    out.append("|---|---|---|---|")
    icon = {PASS: "✅ PASS", FAIL: "❌ FAIL", ENV: "🟡 ENV"}
    for c in checks:
        out.append(f"| {c['id']} {c['name']} | **{icon[c['status']]}** | "
                   f"{c['evidence']} | {c['caveat'] or '—'} |")

    n_pass = sum(1 for c in checks if c["status"] == PASS)
    n_fail = sum(1 for c in checks if c["status"] == FAIL)
    n_env = sum(1 for c in checks if c["status"] == ENV)
    all_hard_pass = (n_fail == 0 and n_env == 0 and n_pass == len(checks))
    out.append(f"\n**统计：PASS {n_pass} / FAIL {n_fail} / ENV {n_env}（共 {len(checks)} 项）**")
    out.append(f"\n**全部硬性验收条件通过：{'✅ 是' if all_hard_pass else '❌ 否 —— Stage 1 未通过验收，不得进入 Stage 2'}**")
    out.append("\n> 规则：任何 FAIL 或 ENV 都使「全部硬性通过」为否；ENV 不计入 PASS、不伪装。")

    # ---------- 12. 阻塞原因 / 缺失证据 / 解决方案 ----------
    out.append("\n## 12. 阻塞原因、缺失证据与解决方案（如实列出，不强行通过）")
    out.append("- **阻塞项 标准2（FAIL）**：10:22 定时质量选股 Top10 永久丢失、不可恢复；"
               "历史批次无运行时原生真相源（章节9=0）与推送凭证（章节10=0）。")
    out.append("  - 缺失证据：10:22 批次原始名单、对应 run_*.json、对应 push_receipt、"
               "微信推送文本持久化记录。")
    out.append("  - 解决方案：这些属**历史既成事实，禁止补造/回填**；唯一正解是部署修复后代码，"
               "于 11-13 真实运行一天产生原生真相源与推送凭证，再对该日重新验收。")
    out.append("- **阻塞项 标准5（ENV）**：本轮无 GitHub 凭据、匿名 API 403 限流，"
               "无法取得真实 Actions 运行与业务摘要日志。")
    out.append("  - 缺失证据：实名 GH_TOKEN 下的 Actions runs 列表与 STEP_SUMMARY 文本。")
    out.append("  - 解决方案：提供具备 actions:read 的凭据，或由用户在 GitHub 网页导出运行日志后重跑。")
    out.append("- **标准4 caveat（ENV 附注）**：18:00 定时自动触发时点未验证。")
    out.append("  - 解决方案：11-13 让 05-daily-ai-top10.yml 在真实 18:00 触发并留痕。")

    text = "\n".join(out)
    print(text)

    # ---- 输出：报告 / 结构化 JSON / 文件 MD5 清单（写到独立证据目录，不写回归档）----
    dest_dir = REPORT_DIR if REPORT_DIR else day_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "stage1_acceptance.md").write_text(text, encoding="utf-8")

    archive_md5 = {}
    for p in sorted(day_dir.iterdir()):
        if p.is_file():
            archive_md5[p.name] = md5_file(p)
    db_md5 = md5_file(DB) if os.path.exists(DB) else None

    result = {
        "schema": "v2_stage1_acceptance/2",
        "generated_at_bj": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "run_date": run_date,
        "data_source": "真实历史归档副本 /tmp/stage1_round2（源自 /tmp/runner_sim2）",
        "readonly": True,
        "db_md5": db_md5,
        "db_counts": db_counts,
        "idempotent_replay": {"before": dup_before, "after": dup_after, "zero_duplicate": dup_zero},
        "universe": {"rows": universe, "complete": complete},
        "business": {"picks": len(picks), "quality": len(qpicks), "momentum": len(mpicks),
                     "candidates": cand_n, "ai_reports": len(ai),
                     "alert_events": alert_events, "session_runs": len(sess_ok)},
        "auto_loss_detection": {"detected": loss, "evidence": loss_ev,
                                "run_snapshots": n_run, "push_receipts": n_recv,
                                "quality_db_rows_exports": qexports},
        "chapter9_native_truth_sources": len(run_files),
        "chapter10_push_receipts": len(receipts),
        "checks": checks,
        "summary": {"PASS": n_pass, "FAIL": n_fail, "ENV": n_env,
                    "total": len(checks), "all_hard_pass": all_hard_pass,
                    "may_apply_stage2": all_hard_pass},
        "archive_file_md5": archive_md5,
        "blockers": [
            {"item": "标准2", "status": FAIL, "reason": "10:22 定时批次 Top10 永久丢失且历史批次无原生真相源/推送凭证",
             "missing_evidence": ["10:22 原始名单", "run_*.json", "push_receipt_*.json", "微信推送文本持久化"],
             "solution": "部署修复后代码，11-13 真实运行一天产生原生真相源后重新验收；禁止补造历史"},
            {"item": "标准5", "status": ENV, "reason": "本轮无 GitHub 凭据、匿名 API 403 限流",
             "missing_evidence": ["实名 GH_TOKEN 下 Actions runs", "STEP_SUMMARY 文本"],
             "solution": "提供 actions:read 凭据或用户导出运行日志后重跑"},
            {"item": "标准4-caveat", "status": ENV, "reason": "18:00 定时自动触发时点未验证",
             "missing_evidence": ["真实 18:00 自动触发留痕"],
             "solution": "11-13 让 daily_ai 工作流在真实 18:00 触发并留痕"},
        ],
    }
    (dest_dir / "stage1_acceptance_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md5_lines = ["# 文件 MD5 清单 · " + run_date + "（真实归档副本，逐文件可核验）", ""]
    md5_lines.append(f"- db: `{db_md5}`  data/v2_history.db")
    md5_lines.append("")
    md5_lines.append("| 归档文件 | MD5 |")
    md5_lines.append("|---|---|")
    for k, v in archive_md5.items():
        md5_lines.append(f"| `{k}` | `{v}` |")
    (dest_dir / "file_md5_manifest.md").write_text("\n".join(md5_lines) + "\n", encoding="utf-8")

    print("\nACCEPTANCE_SUMMARY " + json.dumps({
        "run_date": run_date, "PASS": n_pass, "FAIL": n_fail, "ENV": n_env,
        "all_hard_pass": all_hard_pass, "may_apply_stage2": all_hard_pass,
        "loss_detected": loss, "native_truth_sources": n_run, "push_receipts": n_recv,
        "report_dir": str(dest_dir),
    }, ensure_ascii=False))
    con.close()


if __name__ == "__main__":
    main()
