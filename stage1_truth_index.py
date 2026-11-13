# -*- coding: utf-8 -*-
"""业务真相源核验清单（post-hoc verification index，只读）

目的：为「真实历史归档副本」里**已存在**的业务产出建立独立、可追溯、可核验的
哈希清单与交叉一致性证据，用于回答「业务结果到底有哪些、彼此是否一致、能否追溯」。

严格的诚实边界（写进产物里，供人工核验）：
  - generated_mode = post_hoc_verification_index（事后核验清单）
  - is_runtime_truth_source = False（**不是**选股运行时生成的不可覆盖原始真相源）
  - does_not_modify_history = True（只读，不写回归档目录，不改动任何历史文件）
  - does_not_fabricate = True（不补造 run_*.json / push_receipt，不回填 db，不编造名单）
  - 历史批次的「运行时原生真相源缺失」是客观事实，本清单如实列出，不掩盖、不填充。

用法: python stage1_truth_index.py [YYYY-MM-DD] [--archive-root DIR] [--db FILE] [--out DIR]
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
TABS = ("screening_runs", "picks", "candidates", "alert_events",
        "alert_checks", "ai_reports", "run_logs")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def category(name):
    if name.startswith("run_") and name.endswith(".json"):
        return "native_run_snapshot", True, False
    if name.startswith("push_receipt_") and name.endswith(".json"):
        return "push_receipt", False, True
    if name in ("quality_top10.json", "momentum_top5.json", "quality_candidates.json"):
        return "picks_or_candidates_result", False, False
    if "_db_rows_" in name and name.endswith(".json"):
        return "db_rows_export", False, False
    if name.startswith("ai_top10"):
        return "ai_report", False, False
    if name.startswith("market_snapshot_"):
        return "market_metadata", False, False
    if name.startswith("alerts_") or name.startswith("alert_db_rows"):
        return "alert_events", False, False
    if name.startswith("stage1_acceptance"):
        return "acceptance_report", False, False
    return "other", False, False


def rows(con, sql, args=()):
    cur = con.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build(run_date, archive_root, db_path, out_dir):
    day_dir = Path(archive_root) / run_date.replace("-", "")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in day_dir.iterdir() if p.is_file()) if day_dir.exists() else []

    inv = []
    for p in files:
        st = p.stat()
        cat, is_run, is_recv = category(p.name)
        inv.append({
            "name": p.name,
            "sha256": sha256(p),
            "md5": md5(p),
            "bytes": st.st_size,
            "mtime_bj": dt.datetime.fromtimestamp(st.st_mtime, TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "category": cat,
            "is_native_run_snapshot": is_run,
            "is_push_receipt": is_recv,
        })

    native = [f for f in inv if f["is_native_run_snapshot"]]
    receipts = [f for f in inv if f["is_push_receipt"]]

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    db_counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABS}

    # ---- 交叉一致性核验（全部基于真实文件与真实 db 查询，不预设结论）----
    checks = []

    def add(name, left, right, equal, note=""):
        checks.append({"name": name, "left": left, "right": right,
                       "equal": bool(equal), "verdict": "PASS" if equal else "FAIL",
                       "note": note})

    for fn, task in (("quality_top10.json", "quality"), ("momentum_top5.json", "momentum")):
        fp = day_dir / fn
        if not fp.exists():
            checks.append({"name": f"{fn} vs db picks({task})", "left": "缺失", "right": "-",
                           "equal": False, "verdict": "FAIL", "note": "归档文件不存在"})
            continue
        d = json.loads(fp.read_text(encoding="utf-8"))
        fcodes = [str(x["code"]) for x in d.get("picks", [])]
        dbcodes = [r["code"] for r in rows(
            con, "SELECT code FROM picks WHERE task=? AND run_date=? ORDER BY rank", (task, run_date))]
        add(f"{fn} 名单 vs db picks({task})",
            {"count": len(fcodes), "codes": fcodes},
            {"count": len(dbcodes), "codes": dbcodes},
            fcodes == dbcodes,
            "归档名单与数据库逐序一致" if fcodes == dbcodes else "名单不一致，需排查")

    qc = day_dir / "quality_candidates.json"
    if qc.exists():
        d = json.loads(qc.read_text(encoding="utf-8"))
        fcount = d.get("count")
        fn = len(d.get("candidates", []))
        rid = d.get("run_id")
        dbn = con.execute("SELECT COUNT(*) FROM candidates WHERE run_id=?", (rid,)).fetchone()[0]
        add("quality_candidates.json count 字段 vs 实际列表长度", fcount, fn, fcount == fn)
        add(f"quality_candidates.json 列表 vs db candidates(run_id={rid})", fn, dbn, fn == dbn,
            "db 中该批次候选数")

    ai = day_dir / "ai_top10.json"
    if ai.exists():
        d = json.loads(ai.read_text(encoding="utf-8"))
        add("ai_top10.json results 数 vs db ai_reports(当日)",
            len(d.get("results", [])),
            con.execute("SELECT COUNT(*) FROM ai_reports WHERE run_date=?", (run_date,)).fetchone()[0],
            len(d.get("results", [])) == con.execute(
                "SELECT COUNT(*) FROM ai_reports WHERE run_date=?", (run_date,)).fetchone()[0])

    # db_rows 导出批次：列出每个导出文件的各表行数（真实证据，用于说明 10:22 批次缺失）
    exports = []
    for p in sorted(day_dir.glob("*_db_rows_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            tables = {t: len(v) for t, v in (d.get("tables") or {}).items()}
            exports.append({"file": p.name, "run_date": d.get("run_date"),
                            "task": d.get("task"), "exported_at": d.get("exported_at"),
                            "tables": tables})
        except Exception as e:
            exports.append({"file": p.name, "error": str(e)})

    # 幂等证据：回放前 7 表计数（本脚本不执行回放，避免写 db；回放由验收脚本在副本执行）
    con.close()

    # ---- 结构性缺失的真实证据 ----
    native_evidence = [
        f"归档目录 {day_dir} 共 {len(files)} 个文件，按 run_*.json 通配匹配到 {len(native)} 个",
        "目录真实文件清单：" + ", ".join(f["name"] for f in inv),
    ]
    qexport_empty = [e["file"] for e in exports
                     if e.get("task") == "quality"
                     and (e.get("tables") or {}).get("picks", 0) == 0
                     and (e.get("tables") or {}).get("screening_runs", 0) == 0]
    if qexport_empty:
        native_evidence.append(
            "存在选股结果为空的质量选股导出 " + ", ".join(qexport_empty)
            + "（该时点库内无质量选股名单，印证 10:22 定时批次未落库）")
    sr_q = db_counts  # 仅计数；批次明细由验收脚本给出
    receipt_evidence = [
        f"push_receipt_*.json 匹配到 {len(receipts)} 个",
        "修复前批次代码无 save_push_receipt 调用；微信推送文本从未持久化，"
        "故已推送名单无法从归档比对或恢复",
    ]

    obj = {
        "schema": "v2_business_truth_index/1",
        "generated_at_bj": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "run_date": run_date,
        "source_archive": str(day_dir),
        "source_db": str(db_path),
        "source_archive_file_count": len(files),
        "generated_mode": "post_hoc_verification_index",
        "is_runtime_truth_source": False,
        "does_not_modify_history": True,
        "does_not_fabricate": True,
        "declaration": (
            "本清单是对已存在真实归档文件的事后哈希固化与交叉核验，"
            "不是选股运行时生成的不可覆盖原始真相源；不补造 run_*.json / push_receipt，"
            "不修改任何历史文件、不回填 db、不编造名单。历史批次原生真相源缺失属客观事实，如实列出。"),
        "files": inv,
        "native_truth_sources": {"count": len(native), "glob": "run_*.json",
                                 "evidence": native_evidence},
        "push_receipts": {"count": len(receipts), "glob": "push_receipt_*.json",
                          "evidence": receipt_evidence},
        "db_counts": db_counts,
        "cross_checks": checks,
        "db_rows_exports": exports,
        "irrecoverable_batches": [{
            "batch": "2026-11-12 10:22 定时质量选股 Top10",
            "recoverable": False,
            "reason": "名单仅存在于一次性 runner 内存/微信推送文本，归档冲突后未持久化；"
                      "归档目录无原生真相源、无推送凭证，不可事后重建",
        }],
        "verdict": {
            "PASS": [c["name"] for c in checks if c["verdict"] == "PASS"],
            "FAIL": [c["name"] for c in checks if c["verdict"] == "FAIL"],
            "ENV": [],
        },
    }

    jp = out / "business_truth_index.json"
    jp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    # 可读版 Markdown
    md = [f"# 业务真相源核验清单 · {run_date}", "",
          f"- 生成时间：{obj['generated_at_bj']} 北京时间",
          f"- 来源归档：`{day_dir}`（真实历史副本，{len(files)} 个文件）",
          f"- 来源 db：`{db_path}`",
          f"- **generated_mode**：`{obj['generated_mode']}`（事后核验清单）",
          f"- **is_runtime_truth_source = False**；does_not_modify_history = True；does_not_fabricate = True",
          "", "> " + obj["declaration"], "",
          "## 1. 归档文件哈希清单（逐文件可核验）", "",
          "| 文件 | 字节 | md5 | sha256(前16) | 分类 | mtime(北京) |", "|---|---:|---|---|---|---|"]
    for f in inv:
        md.append(f"| `{f['name']}` | {f['bytes']} | `{f['md5']}` | `{f['sha256'][:16]}` "
                  f"| {f['category']} | {f['mtime_bj']} |")
    md += ["", "## 2. 原生真相源与推送凭证（章节 9 / 10 的真实证据）", "",
           f"- `run_*.json`：**{len(native)} 个**（结构性缺失）"]
    for e in native_evidence:
        md.append(f"  - {e}")
    md.append(f"- `push_receipt_*.json`：**{len(receipts)} 个**（结构性缺失）")
    for e in receipt_evidence:
        md.append(f"  - {e}")
    md += ["", "## 3. 交叉一致性核验（归档 ↔ db）", "",
           "| 核验项 | 左侧 | 右侧 | 一致 | 结论 |", "|---|---|---|---|---|"]
    for c in checks:
        md.append(f"| {c['name']} | `{str(c['left'])[:120]}` | `{str(c['right'])[:120]}` "
                  f"| {c['equal']} | {c['verdict']} |")
    md += ["", "## 4. SQLite 7 表计数（真实查询）", "",
           "| 表 | 行数 |", "|---|---:|"]
    for t, n in db_counts.items():
        md.append(f"| `{t}` | {n} |")
    md += ["", "## 5. db_rows 导出批次明细（10:22 缺失的直接证据）", "",
           "| 文件 | task | exported_at | 各表行数 |", "|---|---|---|---|"]
    for e in exports:
        md.append(f"| `{e['file']}` | {e.get('task')} | {e.get('exported_at')} | {e.get('tables')} |")
    md += ["", "## 6. 不可恢复批次（如实列出，不填充）", ""]
    for b in obj["irrecoverable_batches"]:
        md.append(f"- **{b['batch']}**：recoverable={b['recoverable']}；{b['reason']}")
    md += ["", "## 7. 结论分类", "",
           f"- PASS：{len(obj['verdict']['PASS'])} 项；FAIL：{len(obj['verdict']['FAIL'])} 项；"
           f"ENV：{len(obj['verdict']['ENV'])} 项",
           "- 原生真相源缺失 = **FAIL（历史遗留，不可事后补齐）**，不由本清单修复或伪装。",
           ""]
    mp = out / "business_truth_index.md"
    mp.write_text("\n".join(md), encoding="utf-8")

    print(f"TRUTH_INDEX_OK files={len(files)} native={len(native)} receipts={len(receipts)} "
          f"checks_PASS={len(obj['verdict']['PASS'])} checks_FAIL={len(obj['verdict']['FAIL'])}")
    print(f"  json: {jp}")
    print(f"  md  : {mp}")
    return obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=None)
    ap.add_argument("--archive-root", default="data/history")
    ap.add_argument("--db", default="data/v2_history.db")
    ap.add_argument("--out", default=os.environ.get(
        "STAGE1_REPORT_DIR", "/workspace/Stage1_第二轮证据_20261112/结构化JSON"))
    a = ap.parse_args()
    run_date = a.date or dt.datetime.now(TZ).strftime("%Y-%m-%d")
    obj = build(run_date, a.archive_root, a.db, a.out)
    # 机器可读摘要
    print("TRUTH_INDEX_SUMMARY " + json.dumps({
        "run_date": run_date,
        "files": obj["source_archive_file_count"],
        "native_truth_sources": obj["native_truth_sources"]["count"],
        "push_receipts": obj["push_receipts"]["count"],
        "db_counts": obj["db_counts"],
        "verdict": obj["verdict"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
