# -*- coding: utf-8 -*-
"""Stage 1 · GitHub Actions 原始证据采集器（只读，不触发工作流）。

用途：
  python stage1_collect_actions_evidence.py --date 2026-11-13 --out <证据目录>

原则：
- 只读取公开/已授权的 Runs、Jobs、Logs，不触发、不修改、不删除任何资源。
- Token 仅从 GH_TOKEN 或 GITHUB_TOKEN 环境变量读取，绝不打印或写入文件。
- API/权限/网络失败写入 manifest 并标 ENV；不得伪造空运行记录。
- 本脚本只证明“证据是否采集成功”，不直接判定六项业务验收 PASS。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import zipfile

import requests

API = "https://api.github.com"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"file": str(path), "bytes": len(data), "sha256": sha256_bytes(data)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="北京时间日期 YYYY-MM-DD")
    ap.add_argument("--repo", default=os.environ.get("GH_REPO", "piyangmei01-star/daily_stock_analysis"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    day = dt.date.fromisoformat(args.date)
    start_utc = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone(dt.timedelta(hours=8))).astimezone(dt.timezone.utc)
    end_utc = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone(dt.timedelta(hours=8))).astimezone(dt.timezone.utc)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "stage1-evidence-collector"}
    if token:
        headers["Authorization"] = "Bearer " + token
    sess = requests.Session()
    sess.headers.update(headers)
    manifest = {
        "schema": "stage1_actions_evidence/1",
        "date_bj": args.date,
        "repo": args.repo,
        "window_utc": [start_utc.isoformat(), end_utc.isoformat()],
        "credential_used": bool(token),
        "credential_value_recorded": False,
        "collection_status": "ENV",
        "runs": [],
        "errors": [],
        "files": [],
        "note": "采集成功不等于业务验收 PASS；手动与自动运行按 event 原样保留。",
    }

    try:
        resp = sess.get(f"{API}/repos/{args.repo}/actions/runs", params={"per_page": 100}, timeout=40)
        manifest["runs_http_status"] = resp.status_code
        if resp.status_code != 200:
            raise RuntimeError(f"Runs API HTTP {resp.status_code}: {resp.text[:300]}")
        raw = resp.content
        manifest["files"].append(write_bytes(out / "runs_raw.json", raw))
        data = resp.json()
        selected = []
        for run in data.get("workflow_runs", []):
            created = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if start_utc <= created < end_utc:
                selected.append(run)
        manifest["runs_in_window"] = len(selected)
        if not selected:
            manifest["errors"].append("该北京时间日期窗口内没有 Actions 运行记录；不能以本地测试代替")
    except Exception as exc:
        manifest["errors"].append(f"Runs 采集失败: {type(exc).__name__}: {exc}")
        (out / "evidence_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print("ENV: Runs 证据采集失败，详情见 evidence_manifest.json")
        return 2

    for run in selected:
        rid = str(run["id"])
        rdir = out / rid
        rinfo = {
            "id": run.get("id"), "name": run.get("name"), "event": run.get("event"),
            "status": run.get("status"), "conclusion": run.get("conclusion"),
            "created_at": run.get("created_at"), "run_started_at": run.get("run_started_at"),
            "updated_at": run.get("updated_at"), "head_sha": run.get("head_sha"),
            "html_url": run.get("html_url"), "logs_status": "ENV", "log_markers": {},
        }
        manifest["files"].append(write_bytes(rdir / "run.json", json.dumps(run, ensure_ascii=False, indent=2).encode("utf-8")))
        try:
            jobs = sess.get(f"{API}/repos/{args.repo}/actions/runs/{rid}/jobs", params={"per_page": 100}, timeout=40)
            rinfo["jobs_http_status"] = jobs.status_code
            if jobs.status_code != 200:
                raise RuntimeError(f"Jobs API HTTP {jobs.status_code}: {jobs.text[:200]}")
            manifest["files"].append(write_bytes(rdir / "jobs_raw.json", jobs.content))
        except Exception as exc:
            rinfo["jobs_error"] = f"{type(exc).__name__}: {exc}"
            manifest["errors"].append(f"run {rid} Jobs 采集失败: {exc}")

        try:
            logs = sess.get(f"{API}/repos/{args.repo}/actions/runs/{rid}/logs", timeout=90)
            rinfo["logs_http_status"] = logs.status_code
            if logs.status_code != 200:
                raise RuntimeError(f"Logs API HTTP {logs.status_code}: {logs.text[:200]}")
            log_path = rdir / "logs.zip"
            manifest["files"].append(write_bytes(log_path, logs.content))
            markers = {"VERIFY_OK": 0, "VERIFY_FAIL": 0, "ARCHIVE_OK": 0, "ARCHIVE_FAIL": 0}
            with zipfile.ZipFile(log_path) as zf:
                for member in zf.namelist():
                    if member.endswith("/") or not member.endswith(".txt"):
                        continue
                    text = zf.read(member).decode("utf-8", errors="replace")
                    for marker in markers:
                        markers[marker] += text.count(marker)
                    safe_name = member.replace("/", "__")
                    manifest["files"].append(write_bytes(rdir / "logs_text" / safe_name, text.encode("utf-8")))
            rinfo["log_markers"] = markers
            rinfo["logs_status"] = "COLLECTED"
        except Exception as exc:
            rinfo["logs_error"] = f"{type(exc).__name__}: {exc}"
            manifest["errors"].append(f"run {rid} Logs 采集失败: {exc}")
        manifest["runs"].append(rinfo)

    if selected and all(r.get("jobs_http_status") == 200 and r.get("logs_status") == "COLLECTED" for r in manifest["runs"]):
        manifest["collection_status"] = "COLLECTED"
    elif selected and any(r.get("jobs_http_status") == 200 or r.get("logs_status") == "COLLECTED" for r in manifest["runs"]):
        manifest["collection_status"] = "PARTIAL"
    manifest_path = out / "evidence_manifest.json"
    # 不对 manifest 自身写入伪造哈希；最终哈希由外部总清单计算。
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"collection_status": manifest["collection_status"], "runs": len(selected),
                      "errors": len(manifest["errors"]), "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0 if manifest["collection_status"] == "COLLECTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
