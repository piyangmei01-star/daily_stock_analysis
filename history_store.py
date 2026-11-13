# -*- coding: utf-8 -*-
"""V2 统一历史存储层：SQLite(data/v2_history.db) + 按日归档(data/history/YYYYMMDD/)

2026-11-12 验收失败修复后的设计原则：
- 唯一 Run ID：每次选股生成全局唯一 run_id（微秒 + 随机后缀），同日多次运行互不覆盖
- 立即持久化：选股结果一旦产生，**在微信推送之前**写入不可覆盖独立文件 run_<run_id>.json
- 不可覆盖：真相源用独占创建 + 原子写(临时文件 + os.replace)，目标已存在直接报错
- 可追溯：运行时间/行情日期/数据来源/行情基准/策略版本/评分/名单hash/推送文本hash 全留痕
- 中断可恢复：run_<run_id>.json 落盘即完好；db_rows 未导出也能从 run 快照补建
- 幂等：同一归档按主键 INSERT OR REPLACE/IGNORE 回放，重复记录数=0
- 保留历史：不删除任何 db、归档、旧版本；旧库缺列自动幂等补齐（只加不改不删）
"""
import glob
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
DB_PATH = os.path.join("data", "v2_history.db")
ARCHIVE_ROOT = os.path.join("data", "history")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screening_runs (
    run_id TEXT PRIMARY KEY,            -- 每次运行全局唯一
    task TEXT NOT NULL,                 -- quality / momentum
    run_date TEXT NOT NULL,             -- YYYY-MM-DD（北京时间）
    run_time_bj TEXT NOT NULL,          -- 实际执行时刻
    model_version TEXT NOT NULL,
    status TEXT NOT NULL,               -- success / failed
    universe_count INTEGER, pass1 INTEGER, pass2 INTEGER, pass3 INTEGER,
    market_env_json TEXT,               -- 市场环境快照
    data_updated_at TEXT,               -- 行情数据时间
    error_msg TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS picks (
    pick_id TEXT PRIMARY KEY,           -- {run_id}_{code}
    run_id TEXT NOT NULL,
    task TEXT NOT NULL,
    run_date TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT,
    rank INTEGER,
    score REAL, scores_json TEXT,       -- 综合评分与各因子分
    reason TEXT, risk_tags TEXT,
    close_price REAL,                   -- 当时价格
    snapshot_json TEXT,                 -- 当时完整行情快照（缺失字段存 null，不填假值）
    industry TEXT,
    model_version TEXT,
    data_date TEXT,                     -- 行情日期（新增）
    data_source TEXT,                   -- 行情来源（新增）
    quote_basis TEXT,                   -- 行情基准 intraday/close（新增）
    ai_conclusion TEXT,                 -- 18:00 AI 分析后回填
    created_at TEXT NOT NULL,
    UNIQUE(run_id, code)
);
CREATE TABLE IF NOT EXISTS candidates (
    run_id TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT,
    stage TEXT NOT NULL,                -- fundamental / trend / screened
    close_price REAL, score REAL, industry TEXT,
    snapshot_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, code, stage)
);
CREATE TABLE IF NOT EXISTS alert_events (
    event_id TEXT PRIMARY KEY,
    run_date TEXT NOT NULL, time_bj TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT,
    event_type TEXT NOT NULL,
    observed REAL, threshold REAL, price REAL,
    detail_json TEXT, pushed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_checks (
    check_id TEXT PRIMARY KEY,          -- 微秒粒度，同秒多次检测互不覆盖
    run_date TEXT NOT NULL, time_bj TEXT NOT NULL,
    in_session INTEGER NOT NULL,
    checked_count INTEGER, anomaly_count INTEGER,
    data_ok INTEGER, error_msg TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_reports (
    report_id TEXT PRIMARY KEY,
    run_date TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT,
    direction TEXT,
    score REAL, reason TEXT, risk TEXT,
    evidence_insufficient INTEGER DEFAULT 0,
    model TEXT, raw_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_date, code)
);
CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL, time_bj TEXT NOT NULL,
    task TEXT NOT NULL, level TEXT NOT NULL,   -- INFO/WARN/ERROR
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# 旧库缺列时自动补齐（只 ALTER ADD，绝不改/删已有列与数据）
_PICKS_EXTRA_COLS = {"data_date": "TEXT", "data_source": "TEXT", "quote_basis": "TEXT"}
_PICKS_LEGACY_COLS = ["pick_id", "run_id", "task", "run_date", "code", "name", "rank",
                      "score", "scores_json", "reason", "risk_tags", "close_price",
                      "snapshot_json", "industry", "model_version", "ai_conclusion",
                      "created_at"]


def now_bj():
    return datetime.now(TZ)


def today():
    return now_bj().strftime("%Y-%m-%d")


def yyyymmdd():
    return now_bj().strftime("%Y%m%d")


def _ts():
    return now_bj().strftime("%Y-%m-%d %H:%M:%S")


def sha256_text(s):
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()


def new_run_id(task, run_date=None):
    """每次选股生成全局唯一 Run ID：task_日期_微秒时间_随机后缀。
    同日同任务多次运行必然不同，从根上避免同名覆盖与 ON CONFLICT 覆盖历史。"""
    d = (run_date or today()).replace("-", "")
    return f"{task}_{d}_{now_bj().strftime('%H%M%S%f')}_{uuid.uuid4().hex[:6]}"


def _atomic_write(path, text, immutable=True):
    """原子写：先写唯一 .tmp，fsync 后 os.replace。
    immutable=True 时目标已存在直接抛错（不可覆盖独立文件）；
    进程被中断只会留下 .tmp，绝不产生半截 JSON，也不破坏已完成的归档。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if immutable and p.exists():
        raise FileExistsError(f"归档目标已存在，拒绝覆盖: {p}")
    tmp = p.with_name(p.name + f".{uuid.uuid4().hex[:6]}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return str(p)


def cleanup_stale_tmp():
    """只清理进程中断遗留的未完成 .tmp，绝不删除任何已完成的归档文件。"""
    n = 0
    for fp in glob.glob(os.path.join(ARCHIVE_ROOT, "*", "*.tmp")):
        try:
            os.remove(fp)
            n += 1
        except OSError:
            pass
    return n


def _ensure_columns(conn):
    """旧库幂等补列：只 ADD，不改不删，历史记录 100% 保留。"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(picks)")}
    for c, t in _PICKS_EXTRA_COLS.items():
        if c not in have:
            conn.execute(f"ALTER TABLE picks ADD COLUMN {c} {t}")
    conn.commit()


def latest_run_id(conn, task, run_date=None):
    """当日该任务最新一次运行的 run_id（供下游 AI/验收取最新批次，避免混合多批名单）。"""
    d = run_date or today()
    r = conn.execute(
        "SELECT run_id FROM screening_runs WHERE task=? AND run_date=? "
        "ORDER BY created_at DESC, run_id DESC LIMIT 1", (task, d)).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- 不可覆盖真相源
def finalize_run(conn, task, run_date, run_id, picks, meta, push_text=None):
    """选股结果一旦产生立即持久化为不可覆盖独立文件（**必须在微信推送之前调用**）。

    文件名 run_<run_id>.json，run_id 全局唯一；落盘后重读校验 hash，
    确保即便进程随后中断，这份原始名单也完整可恢复。
    meta 需含: data_date / data_source / quote_basis / model_version / counts / market_env
    """
    codes = [str(r["code"]) for r in picks]
    obj = {
        "schema": "v2_run_snapshot/1",
        "run_id": run_id, "task": task, "run_date": run_date,
        "run_time_bj": meta.get("run_time_bj") or _ts(),
        "data_date": meta.get("data_date"),
        "data_source": meta.get("data_source"),
        "quote_basis": meta.get("quote_basis"),
        "date_source": meta.get("date_source"),
        "model_version": meta.get("model_version"),
        "universe": meta.get("universe"),
        "counts": meta.get("counts"),
        "market_env": meta.get("market_env"),
        "picks": picks,
        "picks_count": len(picks),
        "codes": codes,
        "codes_hash": sha256_text(",".join(codes)),
        "push_text": push_text,
        "push_text_hash": sha256_text(push_text) if push_text is not None else None,
        "persisted_at": _ts(),
        "immutable": True,
    }
    fp = _atomic_write(
        os.path.join(ARCHIVE_ROOT, run_date.replace("-", ""), f"run_{run_id}.json"),
        json.dumps(obj, ensure_ascii=False, indent=2, default=str), immutable=True)
    with open(fp, encoding="utf-8") as f:
        back = json.load(f)
    if back["codes_hash"] != obj["codes_hash"] or back["run_id"] != run_id:
        raise RuntimeError(f"归档落盘校验失败（名单可能被截断）: {fp}")
    return fp, obj


def save_push_receipt(run_date, run_id, push_text, ok, detail=None):
    """推送凭证：独立不可覆盖文件，绑定 run_id 与推送文本 hash。
    用于证明「微信推送名单 == 实际归档名单」。"""
    obj = {
        "schema": "v2_push_receipt/1", "run_id": run_id, "run_date": run_date,
        "pushed_at": _ts(), "push_ok": bool(ok),
        "push_text_hash": sha256_text(push_text),
        "push_text_bytes": len(push_text.encode("utf-8")),
        "detail": detail, "immutable": True,
    }
    return _atomic_write(
        os.path.join(ARCHIVE_ROOT, run_date.replace("-", ""), f"push_receipt_{run_id}.json"),
        json.dumps(obj, ensure_ascii=False, indent=2, default=str), immutable=True)


def verify_run_file(fp):
    """校验一份 run 快照的结构与哈希完整性，返回 (obj, 错误列表)。"""
    try:
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return None, [f"JSON 不可解析: {e}"]
    errs = []
    for k in ("run_id", "task", "run_date", "run_time_bj", "data_date", "data_source",
              "model_version", "picks", "codes", "codes_hash", "persisted_at"):
        if k not in d:
            errs.append(f"缺字段 {k}")
    if errs:
        return d, errs
    if sha256_text(",".join(d["codes"])) != d["codes_hash"]:
        errs.append("codes_hash 与名单不符")
    if d.get("push_text") is not None and sha256_text(d["push_text"]) != d.get("push_text_hash"):
        errs.append("push_text_hash 与推送文本不符")
    if len(d["picks"]) != len(d["codes"]):
        errs.append("picks 条数与 codes 不符")
    return d, errs


# ---------------------------------------------------------------- 归档真相源 / db 重建
# GitHub Actions 每个 runner 都是全新环境，且多个工作流并发向 main 提交归档。
# SQLite 是二进制文件，git rebase 无法自动合并；同名缓存 CSV 也会 add/add 冲突。
# 2026-11-12 事故：质量选股归档因该冲突 push 失败，却被 "|| sleep 5" 吞成 success，
# 导致当日 Top10 未落仓库。据此改为：按日 JSON = 唯一真相源（append-only、时间戳文件名），
# 仓库不再提交 data/v2_history.db 与 data/cache/；db 每次启动从归档幂等重建。
_TABLES = ("screening_runs", "picks", "candidates", "alert_events",
           "alert_checks", "ai_reports", "run_logs")


def _table_cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def export_run_snapshot(conn, run_date, task, run_id=None):
    """导出本次运行的全部 db 行为不可变 JSON（归档真相源）。
    run_id 显式传入最精确；未传时自动取当日该任务最新批次，兼容旧调用点。"""
    rid = run_id or latest_run_id(conn, task, run_date) or f"{task}_{run_date.replace('-', '')}"
    queries = {
        "screening_runs": ("SELECT * FROM screening_runs WHERE run_id=?", (rid,)),
        "picks": ("SELECT * FROM picks WHERE run_id=?", (rid,)),
        "candidates": ("SELECT * FROM candidates WHERE run_id=?", (rid,)),
        "ai_reports": ("SELECT * FROM ai_reports WHERE run_date=?", (run_date,)),
        "alert_checks": ("SELECT * FROM alert_checks WHERE run_date=?", (run_date,)),
        "alert_events": ("SELECT * FROM alert_events WHERE run_date=?", (run_date,)),
        "run_logs": ("SELECT * FROM run_logs WHERE run_date=? AND task=?", (run_date, task)),
    }
    tables = {}
    for t, (sql, args) in queries.items():
        cur = conn.execute(sql, args)
        cols = [d[0] for d in cur.description]
        tables[t] = [dict(zip(cols, row)) for row in cur.fetchall()]
    obj = {"schema": "v2_db_rows/1", "run_date": run_date, "task": task,
           "run_id": rid, "exported_at": _ts(), "tables": tables}
    # 微秒粒度：同一秒内重复运行也产生新文件，绝不覆盖历史归档
    return archive_json(run_date, f"{task}_db_rows_{now_bj().strftime('%H%M%S%f')}.json", obj)


def export_all_history(conn):
    """迁移基线：把库内全部历史按 (日期, 任务) 导出为归档 JSON（一次性）。"""
    dates = set()
    for t in _TABLES:
        if "run_date" in _table_cols(conn, t):
            dates |= {r[0] for r in conn.execute(f"SELECT DISTINCT run_date FROM {t}")}
    tasks = {r[0] for r in conn.execute("SELECT DISTINCT task FROM run_logs")}
    tasks |= {r[0] for r in conn.execute("SELECT DISTINCT task FROM screening_runs")}
    tasks |= {"quality", "momentum", "alert", "daily_ai", "premarket_data"}
    files = []
    for d in sorted(x for x in dates if x):
        for t in sorted(x for x in tasks if x):
            p = export_run_snapshot(conn, d, t)
            with open(p, encoding="utf-8") as fh:
                obj = json.load(fh)
            if any(obj["tables"].values()):
                files.append(p)
            else:
                os.remove(p)          # 不往仓库塞空归档（仅删本次刚生成的空文件）
    return files


def _recover_from_run_snapshots(conn):
    """中断恢复：若某 run_<run_id>.json 已落盘但其 db_rows 导出前进程被杀，
    从这份不可覆盖真相源补建 screening_runs / picks，保证名单永不丢失。
    已存在的 run_id 不覆盖（db_rows 为权威），只补缺失批次。"""
    n = 0
    for fp in sorted(glob.glob(os.path.join(ARCHIVE_ROOT, "*", "run_*.json"))):
        d, errs = verify_run_file(fp)
        if errs or not d:
            continue
        rid = d["run_id"]
        if conn.execute("SELECT 1 FROM screening_runs WHERE run_id=?", (rid,)).fetchone():
            continue
        me = d.get("market_env") or {}
        conn.execute(
            """INSERT OR REPLACE INTO screening_runs(run_id,task,run_date,run_time_bj,
               model_version,status,universe_count,pass1,pass2,pass3,market_env_json,
               data_updated_at,error_msg,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, d["task"], d["run_date"], d["run_time_bj"], d["model_version"],
             "success", d.get("universe"), (d.get("counts") or {}).get("pass1"),
             (d.get("counts") or {}).get("pass2"), (d.get("counts") or {}).get("pass3"),
             json.dumps(me, ensure_ascii=False), d.get("data_date"), None,
             d.get("persisted_at")))
        for r in d["picks"]:
            conn.execute(
                """INSERT OR REPLACE INTO picks(pick_id,run_id,task,run_date,code,name,rank,
                   score,scores_json,reason,risk_tags,close_price,snapshot_json,industry,
                   model_version,data_date,data_source,quote_basis,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"{rid}_{r['code']}", rid, d["task"], d["run_date"], r["code"], r.get("name"),
                 r.get("rank"), r.get("score"),
                 json.dumps(r.get("scores", {}), ensure_ascii=False), r.get("reason"),
                 r.get("risk_tags"), r.get("close_price"),
                 json.dumps(r.get("snapshot", {}), ensure_ascii=False), r.get("industry"),
                 d["model_version"], d.get("data_date"), d.get("data_source"),
                 d.get("quote_basis"), d.get("persisted_at")))
            n += 1
    conn.commit()
    return n


def rebuild_from_archive(conn):
    """从全部按日归档 JSON 幂等重建 db（按路径升序，主键 INSERT OR REPLACE）。"""
    files = sorted(glob.glob(os.path.join(ARCHIVE_ROOT, "*", "*_db_rows_*.json")))
    cols_map = {t: _table_cols(conn, t) for t in _TABLES}
    n = 0
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        for t, rws in (d.get("tables") or {}).items():
            if t not in cols_map or not rws:
                continue
            cols = cols_map[t]
            ph = ",".join(["?"] * len(cols))
            sql = f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES({ph})"
            for r in rws:
                try:
                    conn.execute(sql, [r.get(c) for c in cols])
                    n += 1
                except Exception:
                    pass
    n += _recover_from_run_snapshots(conn)
    # AI 结论的真相源是 ai_top10.json，重建时回填 picks.ai_conclusion
    for fp in sorted(glob.glob(os.path.join(ARCHIVE_ROOT, "*", "ai_top10.json"))):
        try:
            d = json.load(open(fp, encoding="utf-8"))
            for r in d.get("results", []):
                conn.execute(
                    "UPDATE picks SET ai_conclusion=? WHERE run_date=? AND code=? AND task='quality'",
                    (f"{r.get('direction')}(信心分{r.get('score')})", d.get("date"), r.get("code")))
        except Exception:
            pass
    conn.commit()
    return n, len(files)


def _raw_conn(db_path):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    return conn


def get_conn(db_path=DB_PATH, auto_rebuild=True):
    cleanup_stale_tmp()
    conn = _raw_conn(db_path)
    if auto_rebuild:
        try:
            rebuild_from_archive(conn)
        except Exception as e:
            try:
                conn.execute(
                    "INSERT INTO run_logs(run_date,time_bj,task,level,message,created_at) VALUES(?,?,?,?,?,?)",
                    (today(), _ts(), "history_store", "ERROR", f"归档重建失败: {e}"[:2000], _ts()))
                conn.commit()
            except Exception:
                pass
    return conn


def log_event(task, level, message, conn=None):
    own = conn is None
    if own:
        conn = get_conn()
    conn.execute(
        "INSERT INTO run_logs(run_date,time_bj,task,level,message,created_at) VALUES(?,?,?,?,?,?)",
        (today(), _ts(), task, level, str(message)[:2000], _ts()))
    conn.commit()
    if own:
        conn.close()


def save_run(conn, task, run_date, model_version, status, counts=None,
             market_env=None, data_updated_at=None, error_msg=None):
    """登记一次选股运行：每次生成全新 run_id，绝不复用/覆盖历史运行记录。"""
    run_id = new_run_id(task, run_date)
    c = counts or {}
    conn.execute(
        """INSERT INTO screening_runs(run_id,task,run_date,run_time_bj,model_version,status,
           universe_count,pass1,pass2,pass3,market_env_json,data_updated_at,error_msg,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, task, run_date, _ts(), model_version, status,
         c.get("universe"), c.get("pass1"), c.get("pass2"), c.get("pass3"),
         json.dumps(market_env, ensure_ascii=False) if market_env else None,
         data_updated_at, error_msg, _ts()))
    conn.commit()
    return run_id


def save_picks(conn, run_id, task, run_date, model_version, rows, meta=None):
    """保存 Top 名单。INSERT OR IGNORE + UNIQUE(run_id,code)：同批重放重复记录=0。
    meta 写入行情日期/来源/基准，保证每条名单可追溯到具体行情。"""
    meta = meta or {}
    n_new = 0
    for r in rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO picks(pick_id,run_id,task,run_date,code,name,rank,score,
               scores_json,reason,risk_tags,close_price,snapshot_json,industry,model_version,
               data_date,data_source,quote_basis,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{run_id}_{r['code']}", run_id, task, run_date, r["code"], r.get("name"),
             r.get("rank"), r.get("score"),
             json.dumps(r.get("scores", {}), ensure_ascii=False),
             r.get("reason"), r.get("risk_tags"), r.get("close_price"),
             json.dumps(r.get("snapshot", {}), ensure_ascii=False),
             r.get("industry"), model_version,
             meta.get("data_date"), meta.get("data_source"), meta.get("quote_basis"), _ts()))
        n_new += cur.rowcount
    conn.commit()
    return n_new


def save_candidates(conn, run_id, rows):
    n_new = 0
    for r in rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO candidates(run_id,code,name,stage,close_price,score,
               industry,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (run_id, r["code"], r.get("name"), r.get("stage", "screened"),
             r.get("close_price"), r.get("score"), r.get("industry"),
             json.dumps(r.get("snapshot", {}), ensure_ascii=False), _ts()))
        n_new += cur.rowcount
    conn.commit()
    return n_new


def save_alert_check(conn, in_session, checked, anomaly, data_ok, error_msg=None):
    t = now_bj()
    cid = t.strftime("%Y%m%d_%H%M%S%f") + "_" + uuid.uuid4().hex[:4]
    conn.execute(
        """INSERT OR REPLACE INTO alert_checks(check_id,run_date,time_bj,in_session,
           checked_count,anomaly_count,data_ok,error_msg,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (cid, t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S"), int(in_session),
         checked, anomaly, int(data_ok), error_msg, _ts()))
    conn.commit()
    return cid


def save_alert_events(conn, events, pushed=False):
    n_new = 0
    for e in events:
        cur = conn.execute(
            """INSERT OR IGNORE INTO alert_events(event_id,run_date,time_bj,code,name,event_type,
               observed,threshold,price,detail_json,pushed,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (e["event_id"], e["run_date"], e["time_bj"], e["code"], e.get("name"),
             e["event_type"], e.get("observed"), e.get("threshold"), e.get("price"),
             json.dumps(e.get("detail", {}), ensure_ascii=False), int(pushed), _ts()))
        n_new += cur.rowcount
    conn.commit()
    return n_new


def save_ai_report(conn, run_date, r):
    conn.execute(
        """INSERT INTO ai_reports(report_id,run_date,code,name,direction,score,reason,risk,
           evidence_insufficient,model,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(report_id) DO UPDATE SET direction=excluded.direction,
             score=excluded.score, reason=excluded.reason, risk=excluded.risk,
             evidence_insufficient=excluded.evidence_insufficient,
             model=excluded.model, raw_json=excluded.raw_json""",
        (f"{run_date.replace('-', '')}_{r['code']}", run_date, r["code"], r.get("name"),
         r.get("direction"), r.get("score"), r.get("reason"), r.get("risk"),
         int(r.get("evidence_insufficient", 0)), r.get("model"),
         json.dumps(r.get("raw", {}), ensure_ascii=False), _ts()))
    conn.commit()


def get_today_picks(conn, task, run_date, run_id=None):
    """取当日**最新一个批次**的 picks（显式列出旧 17 列，保证下游 analyze_one 解包兼容）。
    多批次共存时不会把不同批名单混在一起。"""
    rid = run_id or latest_run_id(conn, task, run_date)
    cols = ",".join(_PICKS_LEGACY_COLS)
    if rid:
        return conn.execute(
            f"SELECT {cols} FROM picks WHERE run_id=? ORDER BY rank", (rid,)).fetchall()
    return conn.execute(
        f"SELECT {cols} FROM picks WHERE task=? AND run_date=? ORDER BY rank",
        (task, run_date)).fetchall()


def archive_json(run_date, filename, obj, immutable=False):
    """按日归档 JSON。immutable=True 时不可覆盖（用于真相源）；
    默认 False 用于「最新视图」派生文件（可更新，不是真相源）。"""
    d = os.path.join(ARCHIVE_ROOT, run_date.replace("-", ""))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, filename)
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if immutable:
        return _atomic_write(p, text, immutable=True)
    return _atomic_write(p, text, immutable=False)


def archive_text(run_date, filename, text, immutable=False):
    d = os.path.join(ARCHIVE_ROOT, run_date.replace("-", ""))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, filename)
    return _atomic_write(p, text, immutable=immutable)
