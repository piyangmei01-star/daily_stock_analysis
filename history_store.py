# -*- coding: utf-8 -*-
"""V2 统一历史存储层：SQLite(data/v2_history.db) + 按日归档(data/history/YYYYMMDD/)

设计原则：
- append-only：历史记录永不删除、跨日永不覆盖
- 幂等：run_id 由 task+日期决定，同日重复运行 UNIQUE 去重，重复记录数=0
- 独立：不读写 V1 的任何文件与数据表
"""
import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
DB_PATH = os.path.join("data", "v2_history.db")
ARCHIVE_ROOT = os.path.join("data", "history")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screening_runs (
    run_id TEXT PRIMARY KEY,            -- {task}_{YYYYMMDD}，同日幂等
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
    close_price REAL,                   -- 当时价格（盘后任务=当日收盘）
    snapshot_json TEXT,                 -- 当时完整行情快照
    industry TEXT,
    model_version TEXT,
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
    event_id TEXT PRIMARY KEY,          -- {YYYYMMDD}_{HHMMSS}_{code}_{type}
    run_date TEXT NOT NULL, time_bj TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT,
    event_type TEXT NOT NULL,           -- pct_up5/pct_down5/limit_up/limit_down/vol_ratio3
    observed REAL, threshold REAL, price REAL,
    detail_json TEXT, pushed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_checks (
    check_id TEXT PRIMARY KEY,          -- {YYYYMMDD}_{HHMMSS} 每次检测一行
    run_date TEXT NOT NULL, time_bj TEXT NOT NULL,
    in_session INTEGER NOT NULL,        -- 是否交易时段
    checked_count INTEGER, anomaly_count INTEGER,
    data_ok INTEGER, error_msg TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_reports (
    report_id TEXT PRIMARY KEY,         -- {YYYYMMDD}_{code}
    run_date TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT,
    direction TEXT,                     -- 看多/偏多/观望/偏空/高风险
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


def now_bj():
    return datetime.now(TZ)


def today():
    return now_bj().strftime("%Y-%m-%d")


def yyyymmdd():
    return now_bj().strftime("%Y%m%d")


def _ts():
    return now_bj().strftime("%Y-%m-%d %H:%M:%S")


def get_conn(db_path=DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def log_event(task, level, message, conn=None):
    """异常/事件日志：数据获取失败、工作流失败、缺失字段都写这里"""
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
    """登记一次选股运行。同日同任务 run_id 固定 → 重复运行不产生新运行记录"""
    run_id = f"{task}_{run_date.replace('-', '')}"
    c = counts or {}
    conn.execute(
        """INSERT INTO screening_runs(run_id,task,run_date,run_time_bj,model_version,status,
           universe_count,pass1,pass2,pass3,market_env_json,data_updated_at,error_msg,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id) DO UPDATE SET
             run_time_bj=excluded.run_time_bj, status=excluded.status,
             universe_count=excluded.universe_count, pass1=excluded.pass1,
             pass2=excluded.pass2, pass3=excluded.pass3,
             market_env_json=excluded.market_env_json,
             data_updated_at=excluded.data_updated_at, error_msg=excluded.error_msg""",
        (run_id, task, run_date, _ts(), model_version, status,
         c.get("universe"), c.get("pass1"), c.get("pass2"), c.get("pass3"),
         json.dumps(market_env, ensure_ascii=False) if market_env else None,
         data_updated_at, error_msg, _ts()))
    conn.commit()
    return run_id


def save_picks(conn, run_id, task, run_date, model_version, rows):
    """保存 Top 名单。INSERT OR IGNORE：同日重复运行重复记录=0"""
    n_new = 0
    for r in rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO picks(pick_id,run_id,task,run_date,code,name,rank,score,
               scores_json,reason,risk_tags,close_price,snapshot_json,industry,model_version,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{run_id}_{r['code']}", run_id, task, run_date, r["code"], r.get("name"),
             r.get("rank"), r.get("score"),
             json.dumps(r.get("scores", {}), ensure_ascii=False),
             r.get("reason"), r.get("risk_tags"), r.get("close_price"),
             json.dumps(r.get("snapshot", {}), ensure_ascii=False),
             r.get("industry"), model_version, _ts()))
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
    cid = t.strftime("%Y%m%d_%H%M%S")
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


def get_today_picks(conn, task, run_date):
    return conn.execute(
        "SELECT * FROM picks WHERE task=? AND run_date=? ORDER BY rank",
        (task, run_date)).fetchall()


def archive_json(run_date, filename, obj):
    """按日归档 JSON：data/history/YYYYMMDD/xxx.json。历史日期目录只增不改"""
    d = os.path.join(ARCHIVE_ROOT, run_date.replace("-", ""))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, filename)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    return p


def archive_text(run_date, filename, text):
    d = os.path.join(ARCHIVE_ROOT, run_date.replace("-", ""))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, filename)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p
