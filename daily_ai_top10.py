# -*- coding: utf-8 -*-
"""V2 每日 AI 分析：分析当日质量选股 Top10（而非自选股），结果落库+按日归档。

数据流：当日Top10(v2_history.db) → 逐股构造上下文 → DeepSeek 结构化分析
       → ai_reports 表 + data/history/YYYYMMDD/ai_top10.{json,md}
       → 18:00 微信推送（复用 v1.push 的 PUSH_OK 通道，失败显式 exit 1）
说明：DeepSeek 只负责解释与风险分析，不产生任何用于训练的评分/概率。
      推送文本与归档报告同源；推送前先落盘不可覆盖真相源 run_<daily_ai_rid>.json。
"""
import json
import os
import sys

import requests

import morning_push as v1  # 复用已验证的 PUSH_OK 企业微信 text 通道（V1 文件零改动）
import history_store as hs

API_KEY = os.environ.get("OPENAI_API_KEY", "")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("OPENAI_MODEL", "deepseek-chat")
TASK = "daily_ai"

PROMPT_TMPL = """你是A股风险分析助手。以下是今日量化选股模型选出的股票数据（真实行情快照）。
请基于数据做分析，不要编造未提供的指标或新闻。

股票: {name}({code}) 行业: {industry}
当时价格: {price} 当日涨跌幅: {pct}%
量化综合评分: {score}（规则评分，非概率）
因子: {scores}
入选理由: {reason}
风险标签: {risk_tags}
行情快照: {snapshot}

要求：
1. 给出明确分类，五选一: 看多 / 偏多 / 观望 / 偏空 / 高风险
2. 如果数据证据不足必须输出"观望"，并说明缺什么证据
3. 给出0-100的分析信心分（仅表示证据充分度，不是上涨概率）
4. 指出最大的1-2个风险点
5. 严格返回JSON: {{"direction":"...","score":数字,"reason":"100字内","risk":"80字内","evidence_insufficient":true/false}}
只返回JSON，不要其他文字。"""


def analyze_one(row):
    (pick_id, run_id, task, run_date, code, name, rank, score, scores_json, reason,
     risk_tags, close_price, snapshot_json, industry, model_version, ai_conclusion, created_at) = row
    prompt = PROMPT_TMPL.format(
        name=name, code=code, industry=industry or "未知", price=close_price,
        pct=json.loads(snapshot_json).get("pct"), score=score, scores=scores_json,
        reason=reason, risk_tags=risk_tags, snapshot=snapshot_json)
    r = requests.post(f"{BASE_URL}/chat/completions",
                      headers={"Authorization": f"Bearer {API_KEY}"},
                      json={"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.2, "response_format": {"type": "json_object"}},
                      timeout=90)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = json.loads(text[text.find("{"):text.rfind("}") + 1])
    if obj.get("direction") not in ("看多", "偏多", "观望", "偏空", "高风险"):
        obj["direction"] = "观望"
        obj["evidence_insufficient"] = True
    return {"code": code, "name": name, "direction": obj.get("direction"),
            "score": obj.get("score"), "reason": obj.get("reason"), "risk": obj.get("risk"),
            "evidence_insufficient": bool(obj.get("evidence_insufficient")),
            "model": MODEL, "raw": obj}


def main():
    conn = hs.get_conn()
    run_date = hs.today()
    # 只分析当日最新一个批次的名单，避免多批次共存时混合不同批选股结果
    rid = hs.latest_run_id(conn, "quality", run_date)
    rows = hs.get_today_picks(conn, "quality", run_date, run_id=rid)
    print(f"分析批次 run_id={rid}", flush=True)
    if not rows:
        hs.log_event(TASK, "ERROR", f"{run_date} 无质量选股Top10记录，AI分析终止")
        print("NO_TOP10_TODAY")
        sys.exit(1)
    print(f"当日Top10共 {len(rows)} 只，开始 DeepSeek 分析...", flush=True)

    results, failed = [], []
    for row in rows:
        code = row[4]
        try:
            res = analyze_one(row)
            hs.save_ai_report(conn, run_date, res)
            results.append(res)
            print(f"  {code} {res['name']}: {res['direction']} 信心分{res['score']}", flush=True)
        except Exception as e:
            failed.append(code)
            hs.log_event(TASK, "ERROR", f"{code} AI分析失败: {str(e)[:300]}")
            print(f"  {code} 分析失败: {str(e)[:120]}", flush=True)

    if results:
        hs.archive_json(run_date, "ai_top10.json",
                        {"date": run_date, "run_id": rid, "model": MODEL,
                         "count": len(results), "failed": failed, "results": results})
        lines = [f"# {run_date} 质量选股Top10 · AI分析报告", "",
                 f"模型: {MODEL} | 成功 {len(results)} 只 | 失败 {len(failed)} 只",
                 "> AI仅做解释与风险分析，其评分不是上涨概率，不构成投资建议", ""]
        for r in results:
            lines.append(f"## {r['name']}({r['code']}) — {r['direction']}（信心分 {r['score']}）")
            lines.append(f"- 分析: {r['reason']}")
            lines.append(f"- 风险: {r['risk']}")
            if r["evidence_insufficient"]:
                lines.append("- ⚠️ 证据不足，已按要求说明")
            lines.append("")
        p = hs.archive_text(run_date, "ai_top10.md", "\n".join(lines))
        print(f"AI报告已归档: {p}", flush=True)

    # 回填 picks.ai_conclusion（严格限定本批次，不污染历史批次）
    for r in results:
        if rid:
            conn.execute("UPDATE picks SET ai_conclusion=? WHERE run_id=? AND code=?",
                         (f"{r['direction']}(信心分{r['score']})", rid, r["code"]))
    conn.commit()
    hs.log_event(TASK, "INFO", f"AI分析完成: 成功{len(results)}/失败{len(failed)}")
    print(f"AI_DONE 成功{len(results)} 失败{len(failed)}", flush=True)
    hs.export_run_snapshot(conn, run_date, TASK)  # 归档真相源: append-only JSON

    # ---- 18:00 晚间微信推送：复用 9:25 已验证成功的 PUSH_OK 通道 ----
    # 与 quality_screen_v2.py / morning_push_v2.py 完全同构：
    # 推送文本与归档报告同源 → 推送前不可覆盖真相源 → v1.push → 推送凭证
    # → 失败显式 sys.exit(1)（杜绝 Actions 假绿）。V1 三个核心文件零改动。
    if not results:
        hs.log_event(TASK, "ERROR", "AI 分析结果为空，拒绝推送空复盘报告")
        print("AI_NO_RESULT: 无可用 AI 分析，按验收纪律退出码非0", flush=True)
        sys.exit(1)

    lines = [f"🌙 AI 深度复盘 · 质量选股Top{len(results)}  {hs.now_bj().strftime('%m-%d %H:%M')}",
             f"模型: {MODEL} | 成功 {len(results)} 只 | 失败 {len(failed)} 只",
             f"选股批次: {rid}",
             "> AI 仅做解释与风险分析，其评分不是上涨概率，不构成投资建议", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['name']}({r['code']}) — {r['direction']}（信心分 {r['score']}）")
        lines.append(f"   分析: {r['reason']}")
        lines.append(f"   风险: {r['risk']}")
        if r["evidence_insufficient"]:
            lines.append("   ⚠️ 证据不足，已按要求说明")
        lines.append("")
    lines.append(f"完整报告已归档: data/history/{run_date.replace('-', '')}/ai_top10.md")
    push_text = "\n".join(lines)  # 与上方归档 md 同一个 results 列表，名单不重排不重算

    # 真相源：独立的 daily_ai run_id，避免与 9:25 的 quality 凭证同名覆盖
    ai_run_id = hs.new_run_id(TASK, run_date)
    mv = rows[0][14] if len(rows[0]) > 14 and rows[0][14] else MODEL
    meta = {"run_time_bj": str(hs.now_bj()), "model_version": mv,
            "counts": {"ai_ok": len(results), "ai_failed": len(failed)},
            "data_date": None, "data_source": None, "quote_basis": None,
            "date_source": None, "universe": None, "market_env": None}
    qp = os.path.join(hs.ARCHIVE_ROOT, run_date.replace("-", ""), f"run_{rid}.json")
    if os.path.exists(qp):
        with open(qp, encoding="utf-8") as f:
            q = json.load(f)
        for k in ("data_date", "data_source", "quote_basis", "date_source",
                  "universe", "market_env"):
            meta[k] = q.get(k)  # 继承当日选股的真实行情口径，不编造
    ai_picks = [{"code": r["code"], "name": r["name"], "direction": r["direction"],
                 "score": r["score"], "risk": r["risk"]} for r in results]
    run_fp, run_obj = hs.finalize_run(conn, TASK, run_date, ai_run_id, ai_picks, meta,
                                      push_text=push_text)
    print(f"真相源已落盘(推送前): {os.path.basename(run_fp)} run_id={ai_run_id}", flush=True)

    # 与 9:25 通道一致：--no-push 也在真相源落盘之后跳过推送，便于不改归档地做验证
    if "--no-push" in sys.argv:
        print("NO_PUSH_OK（真相源与AI报告均已落盘，按参数跳过微信推送）", flush=True)
        return

    ret = None
    try:
        ret = v1.push(push_text)
        push_ok = True if ret is None else bool(ret)
    except Exception as e:
        push_ok = False
        hs.log_event(TASK, "ERROR", f"微信推送异常: {e}")
    hs.save_push_receipt(run_date, ai_run_id, push_text, push_ok,
                         detail={"picks": len(results), "failed": len(failed),
                                 "source_run_id": rid, "push_return": str(ret)})
    print(f"推送凭证已保存: push_ok={push_ok} run_id={ai_run_id}", flush=True)
    if not push_ok:
        print("PUSH_FAIL: 微信推送失败，按验收纪律退出码非0（真相源与推送凭证已落盘）",
              flush=True)
        sys.exit(1)
    print("PUSH_OK", flush=True)


if __name__ == "__main__":
    main()
