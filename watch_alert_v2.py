# -*- coding: utf-8 -*-
"""V2 自选股异动监控：修复 V1 的 UTC 时区缺陷，新增交易日历、检测日志与事件归档。

V1 缺陷：time.localtime() 在 GitHub 服务器为 UTC → 永远 SKIP_NON_TRADING，从未生效。
V2 修复：
- 统一 zoneinfo Asia/Shanghai 判断时间
- 交易日历（含法定节假日），非交易日直接跳过并留痕
- 每次运行写 alert_checks（是否交易时段/检测数量/异常数量/数据是否成功）
- 异动事件写 alert_events + 按日归档，推送格式与 V1 一致
- 行情改用东财直连接口（逐股查询，避免 efinance 海外不稳定）
"""
import json
import os
import sys

import requests

import history_store as hs

WEBHOOK = os.environ.get("WECHAT_WEBHOOK_URL", "")
WATCHLIST = [c.strip() for c in os.environ.get(
    "STOCK_LIST", "000860,600007,601919,002724,300616,001239,300314,603273,920418,002626,002613,600736,301314,300145,920509,920593"
).split(",") if c.strip()]
CALENDAR_PATH = os.path.join("data", "trade_calendar.json")
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def load_trade_dates():
    """交易日历（含法定节假日），缓存7天；失败时降级为周一~周五并记WARN"""
    import time
    if os.path.exists(CALENDAR_PATH) and time.time() - os.path.getmtime(CALENDAR_PATH) < 7 * 86400:
        try:
            with open(CALENDAR_PATH) as f:
                return set(json.load(f))
        except Exception:
            pass
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        dates = sorted(str(d)[:10] for d in cal.iloc[:, 0])
        os.makedirs("data", exist_ok=True)
        with open(CALENDAR_PATH, "w") as f:
            json.dump(dates, f)
        return set(dates)
    except Exception as e:
        hs.log_event("alert", "WARN", f"交易日历获取失败，降级为周一至周五判断: {e}")
        return None


def session_status(now=None):
    """返回 (是否交易日, 是否交易时段, 说明)。统一北京时间"""
    now = now or hs.now_bj()
    d = now.strftime("%Y-%m-%d")
    hm = now.hour * 100 + now.minute
    dates = load_trade_dates()
    if dates is not None:
        is_day = d in dates
    else:
        is_day = now.weekday() < 5
    in_sess = is_day and ((930 <= hm <= 1130) or (1300 <= hm <= 1500))
    return is_day, in_sess, f"{d} {now.strftime('%H:%M:%S')} 北京时间"


def fetch_stock(code):
    """单股行情：统一数据层多源备用（东财 -> 腾讯 -> 新浪）。

    返回字段与 V1 一致，额外带 source / data_date 用于标注备用数据实际来源与日期。
    全部来源失败返回 None，由 check() 明确记 WARN，不静默忽略。
    """
    import market_data as md
    return md.fetch_stock_quote(code)


def check():
    """逐股检测，返回 (事件列表, 检测成功数量, 错误列表)"""
    import time
    events, checked, errors = [], 0, []
    now = hs.now_bj()
    for code in WATCHLIST:
        q = None
        for attempt in range(3):
            try:
                q = fetch_stock(code)
                break
            except Exception as e:
                err = str(e)[:120]
                time.sleep(1.5)
        if q is None:
            errors.append(code)
            hs.log_event("alert", "WARN", f"{code} 行情获取失败或停牌无数据")
            continue
        checked += 1
        # 启用备用数据源时必须明确标注来源与实际数据日期
        if q.get("source") != "eastmoney_stock_get":
            hs.log_event("alert", "INFO",
                         f"{code} 使用备用行情 source={q.get('source')} "
                         f"data_date={q.get('data_date')}")
        tags = []
        if q["pct"] is None:
            continue
        if q["pct"] >= 9.5:
            tags.append(("limit_up", "🚀 涨停/临停", q["pct"], 9.5))
        elif q["pct"] >= 5:
            tags.append(("pct_up5", "🔥 大涨", q["pct"], 5.0))
        if q["pct"] <= -9.5:
            tags.append(("limit_down", "💥 跌停/临停", q["pct"], -9.5))
        elif q["pct"] <= -5:
            tags.append(("pct_down5", "🌊 大跌", q["pct"], -5.0))
        if q["vol_ratio"] is not None and q["vol_ratio"] >= 3:
            tags.append(("vol_ratio3", f"📣 放量(量比{q['vol_ratio']:.1f})", q["vol_ratio"], 3.0))
        for etype, label, observed, threshold in tags:
            events.append({
                "event_id": f"{now.strftime('%Y%m%d_%H%M%S')}_{code}_{etype}",
                "run_date": now.strftime("%Y-%m-%d"), "time_bj": now.strftime("%H:%M:%S"),
                "code": code, "name": q["name"], "event_type": etype,
                "observed": observed, "threshold": threshold, "price": q["price"],
                "detail": {"label": label, "turnover": q["turnover"], "vol_ratio": q["vol_ratio"],
                           "pct": q["pct"], "prev_close": q["prev_close"]},
                "_label": label})
    return events, checked, errors


def push(text):
    """与 V1 一致的 text 分条推送"""
    plain = text.replace("**", "")
    chunks, cur, size = [], [], 0
    for line in plain.split("\n"):
        b = len(line.encode("utf-8")) + 1
        if size + b > 1900 and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += b
    if cur:
        chunks.append("\n".join(cur))
    ok = True
    for c in chunks:
        r = requests.post(WEBHOOK, json={"msgtype": "text", "text": {"content": c}}, timeout=15)
        try:
            ok = ok and r.json().get("errcode") == 0
        except Exception:
            ok = False
    return ok


def main():
    conn = hs.get_conn()
    force = "--force" in sys.argv
    no_push = "--no-push" in sys.argv
    is_day, in_sess, desc = session_status()
    print(f"[会话] {desc} | 交易日={is_day} 交易时段={in_sess} force={force}", flush=True)

    if not in_sess and not force:
        hs.save_alert_check(conn, in_session=False, checked=0, anomaly=0, data_ok=True,
                            error_msg="非交易时段跳过" if is_day else "非交易日跳过")
        print("SKIP_NON_TRADING（已按北京时间+交易日历判定）", flush=True)
        return

    try:
        events, checked, errors = check()
    except Exception as e:
        hs.save_alert_check(conn, in_session=in_sess, checked=0, anomaly=0, data_ok=False,
                            error_msg=str(e)[:300])
        hs.log_event("alert", "ERROR", f"异动检测失败: {e}")
        raise

    hs.save_alert_check(conn, in_session=in_sess, checked=checked, anomaly=len(events),
                        data_ok=True,
                        error_msg=f"{len(errors)}只行情缺失:{','.join(errors)}" if errors else None)
    print(f"[检测完成] 检测 {checked}/{len(WATCHLIST)} 只 | 异常 {len(events)} 条 | 行情缺失 {len(errors)} 只",
          flush=True)

    pushed = False
    if events and not no_push:
        alerts = []
        for e in events:
            emoji = "🔴" if e["detail"]["pct"] > 0 else "🟢"
            alerts.append(f"{emoji} **{e['name']}**({e['code']}) {e['price']:.2f} "
                          f"{e['detail']['pct']:+.2f}% 换手{e['detail']['turnover']}%\n{e['_label']}")
        content = (f"⚡ **自选股异动警报** {hs.now_bj().strftime('%H:%M')}\n\n"
                   + "\n\n".join(alerts) + "\n\n> ⚠️ 量价异动提醒，注意风险")
        pushed = push(content)
        print("PUSH_OK" if pushed else "PUSH_FAIL", flush=True)

    n = hs.save_alert_events(conn, events, pushed=pushed)
    if events:
        hs.archive_json(hs.today(), f"alerts_{hs.now_bj().strftime('%H%M%S')}.json", events)
    hs.log_event("alert", "INFO",
                 f"检测{checked}只/异常{len(events)}条/新增事件{n}条/推送={pushed}/in_session={in_sess}")
    print(f"ALERT_DONE 新增事件 {n} 条（重复运行为0）", flush=True)


if __name__ == "__main__":
    main()
